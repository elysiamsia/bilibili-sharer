"""B 站视频素材抓取服务——这间「食材厨房」把一条链接变成一份 Markdown 素材：
元数据、字幕、语音转写、弹幕、热门评论，一层层摆好给 LLM 看。

对外符号：
- 抓取原语：extract_bvid / resolve_b23_short / fetch_view /
  load_credential / fetch_subtitle_text / fetch_danmakus / fetch_hot_comments；
- 组装与缓存：format_meta_markdown / build_material_markdown（分段降级 +
  max_chars 截断 + bvid→markdown 的 TTL 缓存）/ search_videos_markdown；
- 语音转写集成：build_material_markdown 的 keyword-only 参数 asr_cfg
  （默认 None = 行为与旧版一致）——只在字幕缺席且 [asr] 配置有效时，
  才下载音频单轨并转写出「## 语音转写（ASR）」段（services/asr.py +
  bili_downloader.download_bilibili_audio）；弹幕/评论段标注「观众反应，非视频内容」。

失败语义（build_material_markdown 的分段降级靠它撑腰）：
- extract_bvid / fetch_view：失败抛 BiliMaterialError，build 对外同样抛（元数据段不降级）；
- fetch_danmakus / fetch_hot_comments：接口或网络失败抛 BiliMaterialError，
  弹幕被关闭抛 BiliDanmakuClosedError（BiliMaterialError 的子类，方便降级文案区分），
  只有「确实没内容」时才返回空列表；
- resolve_b23_short / fetch_subtitle_text：失败不抛，见各自 docstring；
- 字幕 / 弹幕 / 评论三段在 build_material_markdown 里各自 try/except 降级，
  任何单段摔倒都不会连累整体。

MIT 归属：本模块移植/改写自 BiliLearn 的部分（UA/Referer、字幕取轨与正文拼接、
链接解析思路、format_meta_markdown 元数据块、素材分段结构与降级文案、搜索结果字段整理）
来自 xiaoyaya191/bilibili_learning_bot, © 2025 XingYe contributors, MIT。
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from bilibili_api import Credential
from bilibili_api.comment import CommentResourceType, OrderType, get_comments
from bilibili_api.search import SearchObjectType, search_by_type
from bilibili_api.video import DanmakuClosedException, Video

from src.app.plugin_system.api.log_api import get_logger

from .asr import load_asr_settings, transcribe_audio

logger = get_logger("bili_sharer_material")

# 浏览器 User-Agent：把请求打扮成普通 Chrome——B 站接口不喜欢素不相识的空手访客。
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# 默认 Referer：很多 B 站接口会问「你从哪儿来」，这句就是名片。
REFERER = "https://www.bilibili.com"
# 视频页 Referer 模板：取具体视频时就说「我从它的页面来」，通过率更高。
VIDEO_REFERER = "https://www.bilibili.com/video/{vid}"

# 默认请求超时（秒）：每次 httpx 调用都明着带上它，
# 免得对着不回话的接口傻等。
DEFAULT_TIMEOUT = 20.0

# 素材最开头的说明行：告诉 LLM「依据」的优先级——字幕/转写为准，
# 弹幕评论只是观众的碎碎念，别当成视频内容。
MATERIAL_HEADER_NOTE = (
    "> 说明：内容依据请以字幕 / 语音转写（ASR）为准；弹幕与评论仅为观众反应，"
    "不要当作视频内容总结。"
)
# 字幕与转写双双缺席时的专用提示：明说「证据不足，请如实讲」——
# 专防「没有字幕就把观众反应总结成视频内容」那种翻车。
ASR_NO_EVIDENCE_NOTE = (
    "> （本视频无字幕与语音转写：以下弹幕/评论不代表视频内容，请如实说明依据不足）"
)
# ASR 段的外层硬时限 = [asr].timeout_seconds + 这份宽限（算法见 _asr_outer_timeout）。
ASR_GRACE_SECONDS = 60.0

VIEW_API = "https://api.bilibili.com/x/web-interface/view"

# 短链域名：聊天里传来传去的多半是 b23.tv / bili2233.cn 这些小短链。
_SHORT_LINK_HOSTS = ("b23.tv", "bili2233.cn")
# 匹配第一个 http(s) 链接：短链常夹在分享文案里，先把 URL 挑出来。
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
# 认视频号的两个正则：BV 号与 av 号（B 站视频的两张身份证）。
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"(?:^|[/?&=])av(\d+)(?![0-9A-Za-z])", re.IGNORECASE)

# cookie 文件的键 -> Credential 参数的映射（键名大小写照实测来）。
_COOKIE_FILE_KEYS = {
    "sessdata": "SESSDATA",
    "bili_jct": "bili_jct",
    "buvid3": "buvid3",
    "dedeuserid": "DedeUserID",
    "ac_time_value": "ac_time_value",
}
# 需要从配置/文件里凑齐的五项凭据。
_CRED_FIELDS = ("sessdata", "bili_jct", "buvid3", "dedeuserid", "ac_time_value")

# 「已知凭据值」登记册：load_credential 拿到值时写进来，
# 给那些拿不到凭据对象的调用点（fetch_view / fetch_danmakus /
# fetch_hot_comments / search_videos_markdown 里构造 BiliMaterialError 的文本）当兜底橡皮。
# 只登记长度 >= 4 的非空字符串；只用来抹日志和异常文本，绝不外发。
_secrets_lock = threading.Lock()
_secret_values: set[str] = set()


def _register_secret_values(values: Any) -> None:
    """把拿到的凭据值登记进「已知值」名册：只登记长度 >= 4 的非空字符串，重复登记也不碍事。"""
    clean = {v for v in (values or ()) if isinstance(v, str) and len(v) >= 4}
    if clean:
        with _secrets_lock:
            _secret_values.update(clean)


def _registered_secret_values() -> tuple[str, ...]:
    """取出已登记凭据值的快照（测试/诊断用）。"""
    with _secrets_lock:
        return tuple(_secret_values)


class BiliMaterialError(Exception):
    """素材获取失败的统一异常：链接认不出 / 接口报错 / 网络不听话，都归它。"""


class BiliDanmakuClosedError(BiliMaterialError):
    """视频弹幕已被关闭（由 bilibili_api 的 DanmakuClosedException 转写而来）。

    单独成类，是为了让 build_material_markdown 能说清「弹幕已关闭」，
    而不是笼统的「弹幕获取失败」；它仍是 BiliMaterialError 的子类，旧的捕获逻辑照常接得住。
    """


def _headers(vid: str = "") -> dict[str, str]:
    """构造请求头：浏览器 UA + 视频页 Referer（没有 vid 时用默认 Referer）。"""
    return {
        "User-Agent": UA,
        "Referer": VIDEO_REFERER.format(vid=vid) if vid else REFERER,
    }


def _redact(text: Any, credential: Any = None) -> str:
    """把日志/异常文本里的凭据值悄悄抹掉（只用于日志与降级文案，绝不外发）。

    凭据值有两个来源（与 tools 层的素材工具同一口径）：
    - 显式传入的 credential：Credential 对象或 [bili_credential] 配置段（同名字段都认）；
    - 「已知凭据值」登记册：load_credential 写入，兜住那些拿不到凭据对象的构造点
      （fetch_view / fetch_danmakus / fetch_hot_comments / search_videos_markdown）。
    """
    out = str(text)
    values: set[str] = set()
    if credential is not None:
        for field in _CRED_FIELDS:
            value = getattr(credential, field, None)
            if isinstance(value, str) and len(value) >= 4:
                values.add(value)
    values.update(_registered_secret_values())
    # 先替换长值：凭据值彼此重叠时，才不会留下短值的残段。
    for value in sorted(values, key=len, reverse=True):
        out = out.replace(value, "***")
    return out


def _fmt_ts(seconds: Any) -> str:
    """时间戳翻成 MM:SS 的小钟点（字幕正文用来标时刻）。"""
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    minutes, secs = divmod(max(total, 0), 60)
    return f"{minutes:02d}:{secs:02d}"


def extract_bvid(url: str) -> str:
    """从 BV 号 / av 号 / 完整 URL / 带参数的 URL 里认出视频号。

    认不出就抛 BiliMaterialError；b23.tv 短链要由调用方先过一遍 resolve_b23_short 再进来。

    Returns:
        BV 号（如 BV1FQY66iEpk）或 av 号（如 av123456）。
    """
    text = str(url or "").strip()
    if not text:
        raise BiliMaterialError("无法从链接中识别 BV 号: 空链接")
    matched = _BV_RE.search(text)
    if matched:
        return matched.group(1)
    matched = _AV_RE.search(text)
    if matched:
        return "av" + matched.group(1)
    raise BiliMaterialError(f"无法从链接中识别 BV 号: {url}")


def _first_url(text: str) -> str:
    """挑出文本里第一个 http(s) 链接；没有就还你空串。"""
    matched = _URL_RE.search(text or "")
    return matched.group(0) if matched else ""


def _is_short_link(url: str) -> bool:
    """判断这是不是 b23.tv / bili2233.cn 的小短链。"""
    text = str(url or "").strip()
    if not text:
        return False
    parsed = urlparse(text if "://" in text else "https://" + text)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(host == item or host.endswith("." + item) for item in _SHORT_LINK_HOSTS)


async def resolve_b23_short(url: str, timeout: float = 10.0) -> str:
    """把 b23.tv / bili2233.cn 短链还原成它藏起来的终态 URL。

    不是短链、不是 URL、或拿不到终态（网络异常 / 重定向失败）时原样奉还，
    本函数不抛异常。以 GET 为主（HEAD 可能吃 405），GET 失败才退回 HEAD 试一次。

    Args:
        url: 原始链接（哪怕夹在分享文案里也行）。
        timeout: 单次请求的超时秒数。

    Returns:
        终态 URL；实在拿不到时返回原始输入。
    """
    original = str(url or "")
    text = original.strip()
    if not text:
        return original
    target = _first_url(text) or text
    if not _is_short_link(target):
        return original
    if "://" not in target:
        target = "https://" + target
    headers = _headers()
    for method in ("get", "head"):
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=timeout, headers=headers
            ) as client:
                resp = await getattr(client, method)(target)
            final = str(resp.url or "")
            if final:
                return final
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"短链还原失败（{method.upper()}）：{type(exc).__name__}: {exc}")
    return original


async def fetch_view(vid: str) -> dict:
    """取视频的 view 元数据（标题 / UP 主 / 时长 / 数据 / 简介 / 分 P）。

    Args:
        vid: BV 号或 av 号；"av" 开头走 aid= 参数，否则走 bvid= 参数。

    Returns:
        view 接口返回里的 data 字段。

    Raises:
        BiliMaterialError: 参数为空 / 网络异常 / 接口 code != 0。
    """
    key = str(vid or "").strip()
    if not key:
        raise BiliMaterialError("vid 为空，无法请求 view 接口")
    params = {"aid": key[2:]} if key[:2].lower() == "av" else {"bvid": key}
    # 请求 + 解析分开处理：B 站风控时返回的是 HTML（不是 JSON），
    # 直接 resp.json() 会抛一句谁也看不懂的 JSONDecodeError；这里翻译成人话。
    last_error = ""
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=_headers(key)) as client:
                resp = await client.get(VIEW_API, params=params)
            if resp.status_code >= 400:
                last_error = (
                    f"B 站接口返回 HTTP {resp.status_code}（多为风控/限流）"
                )
            else:
                try:
                    payload = resp.json()
                    break
                except Exception:  # noqa: BLE001
                    snippet = (resp.text or "")[:80].replace("\n", " ")
                    last_error = (
                        "B 站返回的不是 JSON（常见于风控 412 页面）：" + snippet
                    )
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt == 1:
            # 风控/抖动往往一两秒后就缓过来，给一次重试机会再放弃
            await asyncio.sleep(1.2)
    else:
        raise BiliMaterialError(
            _redact(
                "请求 view 接口失败：" + last_error
                + "（若一直这样：稍等再试，或在 [bili_credential] 配好登录凭据可降低风控概率）",
                None,
            )
        )
    if not isinstance(payload, dict):
        raise BiliMaterialError("view 接口返回不是 JSON 对象")
    if payload.get("code") != 0:
        raise BiliMaterialError(
            f"view 接口返回错误: code={payload.get('code')} message={payload.get('message')}"
        )
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _cfg_field(cred_cfg: Any, name: str) -> str:
    """兼容 pydantic 配置对象与 dict 的取值方式，统一转成去掉空白的字符串。"""
    if cred_cfg is None:
        return ""
    if isinstance(cred_cfg, dict):
        value = cred_cfg.get(name, "")
    else:
        value = getattr(cred_cfg, name, "")
    if value is None:
        return ""
    return str(value).strip()


def _read_cookie_file(path: str) -> dict[str, str]:
    """读 BiliLearn 面板导出的 cookie 文件并映射键名；只记路径和字段个数，绝不记值。"""
    if not path:
        return {}
    try:
        if not os.path.isfile(path):
            logger.warning(f"cookie 文件不存在，忽略：{path}")
            return {}
        with open(path, "r", encoding="utf-8-sig") as fp:
            raw = json.load(fp)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"cookie 文件读取失败（忽略）：{path} -> {type(exc).__name__}: {exc}")
        return {}
    if not isinstance(raw, dict):
        logger.warning(f"cookie 文件内容不是 JSON 对象，忽略：{path}")
        return {}
    values: dict[str, str] = {}
    for name, file_key in _COOKIE_FILE_KEYS.items():
        value = raw.get(file_key)
        if isinstance(value, str) and value.strip():
            values[name] = value.strip()
    logger.info(f"cookie 文件读取完成：{path}（有效字段 {len(values)} 个）")
    return values


def load_credential(cred_cfg: Any) -> Credential | None:
    """用插件配置拼出 bilibili_api 的 Credential。

    优先级：配置字段 > bililearn_cookie_file 文件（逐字段兜底）；两边都取不到值 → None（匿名访问）。
    路径支持 %VAR% 展开（os.path.expandvars）。全程只记录来源和字段个数，**绝不记录 cookie 值**。

    Args:
        cred_cfg: cfg.bili_credential（或同名字段的 dict）。

    Returns:
        Credential 或 None。
    """
    values = {name: _cfg_field(cred_cfg, name) for name in _CRED_FIELDS}
    cookie_file = os.path.expandvars(
        _cfg_field(cred_cfg, "bililearn_cookie_file").strip().strip('"').strip("'")
    )
    file_values: dict[str, str] = {}
    if cookie_file and not all(values.values()):
        file_values = _read_cookie_file(cookie_file)

    merged = {name: values[name] or file_values.get(name, "") for name in _CRED_FIELDS}
    if not any(merged.values()):
        logger.info("未配置 B 站凭据，按匿名访问")
        return None

    # 先把已知凭据值登记进名册：往后哪怕异常发生在拿不到凭据对象的地方
    # （连 Credential 构造本身失败也算），文本都能经 _redact 兜底抹掉
    _register_secret_values(merged.values())

    credential = Credential(
        sessdata=merged["sessdata"] or None,
        bili_jct=merged["bili_jct"] or None,
        buvid3=merged["buvid3"] or None,
        dedeuserid=merged["dedeuserid"] or None,
        ac_time_value=merged["ac_time_value"] or None,
    )
    sources = []
    if any(values.values()):
        sources.append("配置字段")
    if file_values:
        sources.append(f"cookie 文件 {cookie_file}")
    logger.info(f"已加载 B 站凭据（来源：{' + '.join(sources) or '空'}）")
    return credential


def _credential_cookie(credential: Any) -> str:
    """把 Credential 拼成 Cookie 请求头字符串——只进请求头，绝不写日志。"""
    if credential is None:
        return ""
    pairs = []
    for cookie_key, field in (
        ("SESSDATA", "sessdata"),
        ("bili_jct", "bili_jct"),
        ("buvid3", "buvid3"),
        ("DedeUserID", "dedeuserid"),
    ):
        value = getattr(credential, field, None)
        if isinstance(value, str) and value:
            pairs.append(f"{cookie_key}={value}")
    return "; ".join(pairs)


def _subtitle_track_rank(track: dict) -> int:
    """给字幕轨打优先级分：中文人工 3 > 中文 AI 2 > 其它 zh* 1 > 其它 0，分高的先选。"""
    lan = str(track.get("lan") or "").strip().lower()
    doc = str(track.get("lan_doc") or "")
    is_zh = lan.startswith("zh") or lan.startswith("ai-zh") or any(
        key in doc for key in ("中文", "简体", "繁體", "繁体")
    )
    if not is_zh:
        return 0
    is_ai = "ai" in lan or bool(track.get("ai_status")) or any(
        key in doc for key in ("自动生成", "自动翻译", "AI")
    )
    if lan.startswith("zh") and not is_ai:
        return 3
    if is_ai:
        return 2
    return 1


def _pick_subtitle_track(tracks: Any) -> dict | None:
    """按优先级挑一条字幕轨：中文人工 > 中文 AI > 其它 zh*（同级取靠前的）。"""
    best: dict | None = None
    best_rank = 0
    for track in tracks or []:
        if not isinstance(track, dict):
            continue
        rank = _subtitle_track_rank(track)
        if rank > best_rank:
            best, best_rank = track, rank
    return best


def _join_subtitle_body(body: Any, include_timestamps: bool) -> str:
    """拼接字幕正文；include_timestamps 为真时每行戴上 [MM:SS] 的小钟。"""
    lines = []
    for seg in body or []:
        if not isinstance(seg, dict):
            continue
        content = str(seg.get("content") or "").strip()
        if not content:
            continue
        if include_timestamps:
            lines.append(f"[{_fmt_ts(seg.get('from', 0))}] {content}")
        else:
            lines.append(content)
    return "\n".join(lines).strip()


async def fetch_subtitle_text(bvid: str, credential: Any, include_timestamps: bool) -> tuple[bool, str]:
    """抓取视频字幕正文（挑中文轨：人工 > AI > 其它 zh*）。

    注意：本版本 bilibili_api 的 Video.get_subtitle() 必须传 cid，所以内部先取
    cid（分 P 0）再取轨；subtitle_url 用 httpx 带着 cookie 去拉。

    Args:
        bvid: BV 号。
        credential: Credential 或 None（匿名；AI 字幕通常要登录才肯给）。
        include_timestamps: True 时每行带 [MM:SS] 前缀。

    Returns:
        (是否成功, 正文)；没有轨或任何失败一律 (False, "")，不抛。
    """
    try:
        video = Video(bvid=bvid, credential=credential)
        cid = await video.get_cid(0)
        info = await video.get_subtitle(cid=cid) or {}
        tracks = info.get("subtitles") if isinstance(info, dict) else None
        track = _pick_subtitle_track(tracks)
        if not track:
            logger.info(f"视频 {bvid} 无可用中文字幕轨")
            return (False, "")

        sub_url = str(track.get("subtitle_url") or track.get("subtitle_url_v2") or "").strip()
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url  # 协议相对地址（"//xxx" 开头）给它补上 https
        if not sub_url:
            logger.info(f"视频 {bvid} 字幕轨 {track.get('lan')!r} 无 subtitle_url")
            return (False, "")

        headers = _headers(bvid)
        cookie = _credential_cookie(credential)
        if cookie:
            headers["Cookie"] = cookie
        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, headers=headers, follow_redirects=True
        ) as client:
            resp = await client.get(sub_url)
            payload = resp.json()

        body = payload.get("body") if isinstance(payload, dict) else None
        text = _join_subtitle_body(body, include_timestamps)
        if not text:
            logger.info(f"视频 {bvid} 字幕轨 {track.get('lan')!r} 正文为空")
            return (False, "")
        logger.info(f"视频 {bvid} 字幕轨 {track.get('lan')!r} 取到 {len(text)} 字")
        return (True, text)
    except Exception as exc:  # noqa: BLE001
        raw = _redact(f"{type(exc).__name__}: {exc}", credential)
        # 无登录凭据时 bilibili_api 会抛 CredentialNoSessdataException——这在匿名访问下是正常的，
        # 单独给一句人话提示，免得用户以为是插件坏了
        if "CredentialNoSessdata" in type(exc).__name__ or "sessdata" in raw.lower():
            logger.info(
                f"获取字幕跳过（{bvid}）：未配置 B 站登录凭据（[bili_credential] 的 sessdata / "
                "bililearn_cookie_file 任一），匿名访问拿不到字幕——这是正常现象；"
                "想要字幕就填一个凭据文件或 SESSDATA，不想配也没关系，会自动走弹幕/评论或语音转写。"
            )
        else:
            logger.warning(f"获取字幕失败（{bvid}）：{raw}")
        return (False, "")


async def fetch_danmakus(bvid: str, limit: int) -> list[dict]:
    """抓弹幕（第 0 个分 P），截取前 limit 条。

    库语义（bilibili_api video.py 的 get_danmakus，v17.4.2 实测）：from_seg 默认 0、
    to_seg 默认 duration // 360 + 1（每段 6 分钟、闭区间）——默认已覆盖该分 P 全部分段，
    所以这里不传 from_seg / to_seg（自己收窄反而会漏弹幕）；limit 只管取回后截条数。
    test_material.py 有专门的回归断言（case_fetch_danmakus_default_range）把这个钉牢。

    Args:
        bvid: BV 号。
        limit: 条数上限（<= 0 直接给空列表）。

    Returns:
        [{"dm_time": float, "text": str}, ...]；没有弹幕时是空列表。

    Raises:
        BiliDanmakuClosedError: 弹幕已被关闭（BiliMaterialError 的子类）。
        BiliMaterialError: 其它接口/网络异常。
    """
    if limit <= 0:
        return []
    try:
        danmakus = await Video(bvid=bvid).get_danmakus(0)
    except DanmakuClosedException as exc:
        # 库收到「弹幕被关闭」的应答（b"\x10\x01"）时抛这个异常：单独认出来，好让降级文案分得清。
        raise BiliDanmakuClosedError(_redact(f"弹幕已关闭（{bvid}）：{exc}")) from exc
    except Exception as exc:  # noqa: BLE001
        # 异常抛出前先脱敏（本函数拿不到凭据对象，登记册兜底）。
        raise BiliMaterialError(
            _redact(f"获取弹幕失败（{bvid}）：{type(exc).__name__}: {exc}")
        ) from exc

    out: list[dict] = []
    for danmaku in danmakus or []:
        text = str(getattr(danmaku, "text", "") or "").strip()
        if not text:
            continue
        try:
            dm_time = float(getattr(danmaku, "dm_time", 0.0) or 0.0)
        except (TypeError, ValueError):
            dm_time = 0.0
        out.append({"dm_time": dm_time, "text": text})
        if len(out) >= limit:
            break
    return out


async def fetch_hot_comments(aid: int, limit: int) -> list[dict]:
    """抓热门评论（order=OrderType.LIKE），截取前 limit 条。

    Args:
        aid: 视频 avid（view 接口 data.aid 里那个数）。
        limit: 条数上限（<= 0 直接给空列表）。

    Returns:
        [{"uname": str, "like": int, "message": str}, ...]；没有评论时是空列表。

    Raises:
        BiliMaterialError: 接口/网络异常。
    """
    if limit <= 0:
        return []
    try:
        payload = await get_comments(
            aid, CommentResourceType.VIDEO, page_index=1, order=OrderType.LIKE
        )
    except Exception as exc:  # noqa: BLE001
        # 同样是抛出前先脱敏：登记册是最后那块橡皮。
        raise BiliMaterialError(
            _redact(f"获取热门评论失败（aid={aid}）：{type(exc).__name__}: {exc}")
        ) from exc

    replies = payload.get("replies") if isinstance(payload, dict) else None
    out: list[dict] = []
    for item in replies or []:
        if not isinstance(item, dict):
            continue
        message = str((item.get("content") or {}).get("message") or "").strip()
        if not message:
            continue
        try:
            like = int(item.get("like") or 0)
        except (TypeError, ValueError):
            like = 0
        uname = str((item.get("member") or {}).get("uname") or "匿名")
        out.append({"uname": uname, "like": like, "message": message})
        if len(out) >= limit:
            break
    return out


# ================================================================ 组装 / 缓存 / 搜索

# 素材 TTL 缓存：(bvid, 变体) -> markdown（600 秒过期、最多存 50 条）。
# 复合键的好处：同一 bvid 的不同参数组合（开关 / 条数 / 截断 / 页码 / 有无凭据）各占一格、
# 互不牵连——群聊里两条链路参数不同地交替调用，也不会互相挤掉对方、害得反复联网。
CACHE_TTL_SECONDS = 600.0
CACHE_MAX_ENTRIES = 50
# 搜索结果简介的截断长度：太长会淹了聊天上下文，先限个短。
SEARCH_DESC_MAX_CHARS = 120
# 搜索接口单页的条数上限：实测 page_size 最大就是 42。
SEARCH_PAGE_SIZE_MAX = 42
# 给搜索条目拼链接用的模板。
SEARCH_VIDEO_URL = "https://www.bilibili.com/video/{bvid}"

_cache_lock = threading.Lock()
# 缓存格子：(bvid, 参数变体) -> (到期时间, markdown)。
_material_cache: dict[tuple[str, str], tuple[float, str]] = {}
# 单调时钟：测试想拨表，替换 material._clock 就好。
_clock = time.monotonic


def _now() -> float:
    """当前单调时间：全模块唯一的时钟入口，测试想换表就换它。"""
    return _clock()


def _as_int(value: Any, default: int = 0) -> int:
    """宽容取整：None / 数字字符串 / 浮点都能接住，脏数据不闹异常，接不住就用默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _exc_text(exc: Any, credential: Any = None) -> str:
    """把异常转成能显示的文本，顺手抹掉可能混在里面的凭据值（降级文案的兜底工序）。"""
    return _redact(f"{type(exc).__name__}: {exc}", credential)


# ---------------------------------------------------------------- TTL 缓存


def _cache_variant(
    include_danmaku: Any,
    include_comments: Any,
    include_timestamps: Any,
    danmaku_limit: Any,
    comment_limit: Any,
    max_chars: Any,
    credential: Any,
    page: Any = 1,
    asr_enabled: Any = False,
) -> str:
    """缓存的「变体键」：素材长什么样全由这些入参决定，参数不同就不能复用同一份 markdown。

    包含分 P 页码 p=：先请求 p=2 再来 p=1，链接页码不同，不能互相顶替；
    也包含 asr 开关位：开与关的产物可能多出转写段，同样不能互相顶替。
    """
    page_no = _as_int(page, 1)
    return (
        f"d{int(bool(include_danmaku))}c{int(bool(include_comments))}t{int(bool(include_timestamps))}"
        f"dl{_as_int(danmaku_limit)}cl{_as_int(comment_limit)}mc{_as_int(max_chars)}"
        f"cr{int(credential is not None)}p{page_no if page_no > 0 else 1}"
        f"ar{int(bool(asr_enabled))}"
    )


def _asr_enabled_flag(asr_cfg: Any) -> bool:
    """看 [asr] 开没开：只认 enabled 位（给缓存变体用），dict / 配置对象都兼容。"""
    if asr_cfg is None:
        return False
    if isinstance(asr_cfg, dict):
        return bool(asr_cfg.get("enabled", False))
    return bool(getattr(asr_cfg, "enabled", False))


def _cache_key(bvid: Any, variant: Any = "") -> tuple[str, str]:
    """缓存主键 = bvid + 参数变体的复合键：不同变体各占一格，互不牵连失效。"""
    return (str(bvid or "").strip(), str(variant or ""))


def _cache_get(bvid: str, variant: str = "", now: float | None = None) -> str | None:
    """读素材缓存：没命中 / 已过期就返回 None（只清理自己这一格，不动同 bvid 的其他变体）。"""
    key = _cache_key(bvid, variant)
    if not key[0]:
        return None
    moment = _now() if now is None else float(now)
    with _cache_lock:
        entry = _material_cache.get(key)
        if entry is None:
            return None
        expire_at, markdown = entry
        if moment >= expire_at:
            _material_cache.pop(key, None)
            return None
        return markdown


def _cache_put(bvid: str, markdown: str, variant: str = "", now: float | None = None) -> None:
    """写素材缓存（TTL 600 秒）：超过条数上限时，按写入顺序请最旧的条目离场。"""
    key = _cache_key(bvid, variant)
    if not key[0]:
        return
    moment = _now() if now is None else float(now)
    with _cache_lock:
        _material_cache.pop(key, None)
        while len(_material_cache) >= CACHE_MAX_ENTRIES:
            oldest = next(iter(_material_cache))
            _material_cache.pop(oldest, None)
        _material_cache[key] = (moment + CACHE_TTL_SECONDS, markdown)


def _cache_size() -> int:
    """当前缓存条数（测试/诊断用的窥一眼）。"""
    with _cache_lock:
        return len(_material_cache)


def _cache_clear() -> None:
    """清空素材缓存（测试或想主动刷新时用）。"""
    with _cache_lock:
        _material_cache.clear()


# ---------------------------------------------------------------- 元数据块


def _fmt_duration(seconds: Any) -> str:
    """时长翻成 H:MM:SS；不足一小时就写 M:SS（与上游 BiliLearn 同名函数口径一致）。"""
    total = max(_as_int(seconds), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _fmt_count(value: Any) -> str:
    """给计数加千分位；脏数据一律按 0 算——元数据块绝不能被单个字段绊倒。"""
    return f"{max(_as_int(value), 0):,}"


def format_meta_markdown(view: dict, url: str) -> str:
    """把 view 元数据整理成 Markdown 信息块。

    字段、顺序、千分位都沿用 BiliLearn 同名函数的样子，另外对脏数据
    （比如计数被写成字符串）做了容错，让整块不至于因一处坏值而塌掉。

    Args:
        view: fetch_view 返回的 data。
        url: 展示用链接（build_material_markdown 会递来还原后的终态链接）。

    Returns:
        Markdown 元数据块：标题 / UP 主 / 链接 / 时长 / 播放 / 点赞 / 投币 /
        收藏 / 弹幕 / 分区 / 发布时间 / 分 P 数；有简介就加简介段，多 P 就加分 P 列表。
    """
    meta = view if isinstance(view, dict) else {}
    owner = meta.get("owner") if isinstance(meta.get("owner"), dict) else {}
    stat = meta.get("stat") if isinstance(meta.get("stat"), dict) else {}
    pages = meta.get("pages") if isinstance(meta.get("pages"), list) else []
    lines = [
        f"# {meta.get('title', '')}",
        "",
        f"- **UP 主**：{owner.get('name', '')}",
        f"- **链接**：{url}",
        f"- **时长**：{_fmt_duration(meta.get('duration', 0))}",
        f"- **播放**：{_fmt_count(stat.get('view', 0))}",
        f"- **点赞**：{_fmt_count(stat.get('like', 0))}",
        f"- **投币**：{_fmt_count(stat.get('coin', 0))}",
        f"- **收藏**：{_fmt_count(stat.get('favorite', 0))}",
        f"- **弹幕**：{_fmt_count(stat.get('danmaku', 0))}",
        f"- **分区**：{meta.get('tname', '')}",
        f"- **发布时间**：{meta.get('pubdate', '')}",
        f"- **分 P 数**：{len(pages)}",
        "",
    ]
    desc = str(meta.get("desc") or "").strip()
    if desc:
        lines += ["## 视频简介", "", desc, ""]
    if len(pages) > 1:
        lines += ["## 分 P 列表", ""]
        for index, page in enumerate(pages, 1):
            part = page.get("part", "") if isinstance(page, dict) else ""
            duration = page.get("duration", 0) if isinstance(page, dict) else 0
            lines.append(f"{index}. P{index} · {part}（{_fmt_duration(duration)}）")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 素材组装


def _link_page(link: str) -> int:
    """取链接里的分 P 页码 p=（没有 / 非法 / <=1 都按第 1 页算）。"""
    text = str(link or "")
    if "://" not in text:
        return 1
    page = (parse_qs(urlparse(text).query).get("p") or [""])[0]
    return int(page) if page.isdigit() and int(page) > 1 else 1


def _canonical_link(link: str, bvid: str) -> str:
    """把素材展示链接收拾干净：B 站视频链接丢掉分享/追踪参数（buvid / mid / share_session_id…），
    只留分 P 参数 p=；裸 BV/av 号补成规范链接；别的站点的链接原样保留。

    为什么要收：QQ/App 分享的链接（含 b23.tv 还原结果）挂着分享者的 buvid / mid 等标识，
    原样写进素材会把这些标识一并带进 LLM 上下文，还添一长串噪声。
    """
    text = str(link or "").strip()
    canonical = f"https://www.bilibili.com/video/{bvid}"
    if "://" not in text:
        return canonical
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if not host:
        return canonical
    if not (host == "bilibili.com" or host.endswith(".bilibili.com")):
        return text
    if "/video/" not in parsed.path:
        return canonical
    page = _link_page(text)
    return f"{canonical}?p={page}" if page > 1 else canonical


def _truncate_markdown(markdown: str, max_chars: Any) -> str:
    """按 max_chars 截断素材，并追加一行说明（说明行也计入上限）。

    max_chars <= 0 视为不限幅（不然调用方传个 0，输出就被清空了）；
    上限小到连说明行都放不下时，只做硬截断——先保住「输出不超过上限」这个底线。
    """
    limit = _as_int(max_chars)
    if limit <= 0 or len(markdown) <= limit:
        return markdown
    note = f"\n\n> （素材超过 {limit} 字符上限，已截断）"
    if limit <= len(note) - 2:  # 去掉前导的两个换行，按说明行自身的长度来比较
        return markdown[:limit]
    keep = max(limit - len(note), 0)
    body = markdown[:keep].rstrip()
    return f"{body}{note}" if body else note.strip()


def _asr_outer_timeout(settings: Any) -> float:
    """ASR 整段的外层硬时限 = [asr].timeout_seconds + 60 秒宽限。

    音频下载没有单一请求级超时兜底、转写上传也可能慢，整段不能把聊天工具调用拖垮，
    所以用这个数字封顶。单独抽成函数 = 口径只写在一处；测试替换本模块属性即可注入小值验证超时。
    """
    return float(getattr(settings, "timeout", 120.0)) + ASR_GRACE_SECONDS


async def _run_asr_segment(
    bvid: str, page_no: int, asr_cfg: Any, settings: Any, credential: Any
) -> tuple[bool, str]:
    """下载音频单轨、递给 ASR 转写：返回 (是否成功, 正文或一行降级文案)，绝不向外抛。

    bili_downloader 放在函数里 import：两个模块互相引用（downloader 模块级 import material），
    函数级 import 刚好打破循环；测试也能替换模块属性注入离线 stub。
    credential 原样转交给下载器：会员/登录可见视频的音频要凭票入场（None = 匿名）。
    音频临时文件在 finally 里删除（cleanup_temp 幂等不抛）——哪怕外层超时取消本协程，
    finally 照样执行，不会留下孤儿文件。
    """
    try:
        from . import bili_downloader  # noqa: PLC0415（延迟 import：打破循环依赖）
    except Exception as exc:  # noqa: BLE001
        return (False, f"> （语音转写失败：下载模块不可用：{_exc_text(exc, credential)}）")
    audio_path = None
    try:
        audio_path, _meta = await bili_downloader.download_bilibili_audio(
            bvid, page_num=page_no, cfg=asr_cfg, credential=credential
        )
        ok, text = await transcribe_audio(audio_path, settings)
        if ok and text:
            return (True, text)
        return (False, "> （语音转写失败：ASR 服务未返回有效文本，详见日志）")
    except Exception as exc:  # noqa: BLE001（契约异常 / 网络 / 磁盘的意外都在这统一降级）
        logger.warning(f"语音转写段失败（{bvid}）：{_exc_text(exc, credential)}")
        return (False, f"> （语音转写失败：{_exc_text(exc, credential)}）")
    finally:
        if audio_path is not None:
            bili_downloader.cleanup_temp(audio_path)  # _unlink 的承诺：文件不存在、删除失败，都不抛


async def build_material_markdown(
    url: str,
    *,
    include_danmaku: bool,
    include_comments: bool,
    include_timestamps: bool,
    danmaku_limit: int,
    comment_limit: int,
    max_chars: int,
    credential: Any,
    asr_cfg: Any = None,
) -> str:
    """抓取并组装一份视频素材 Markdown：元数据 + 字幕 + 语音转写 + 弹幕 + 热门评论。

    结构是这样排的：头部说明行 → 元数据块 →「## 字幕全文」→ 字幕缺席且 [asr].enabled
    时为「## 语音转写（ASR）」→ 字幕与转写都没有时的一行专用提示 →
    「## 弹幕精选（观众反应，非视频内容）」（include_danmaku）→
    「## 热门评论（观众反应，非视频内容）」（include_comments），末尾按 max_chars 截断。

    每段自己 try/except，一段摔倒不连累别人。字幕失败 → 「> （字幕获取失败：…）」；
    没字幕轨 → 「> （未获取到字幕，可能无 CC 字幕或需要登录）」。

    ASR 段只在字幕缺席且 asr_cfg 的 enabled 为真时出现：enabled 但缺 key →
    「> （语音转写未启用…）」；超时长 → 「> （P{n}/全片时长 …跳过语音转写）」（按目标分 P 判）；
    下载/转写失败 → 「> （语音转写失败：…）」；整段超过 timeout_seconds+60 秒 →
    「> （语音转写超时…）」；成功 → 直接放转写正文。

    字幕与转写都没有正文时 → 「> （本视频无字幕与语音转写：…）」，提醒 LLM 别把观众
    反应当视频内容。弹幕失败/已关闭/空、评论失败/空，也各有自己的一行降级文案；
    只有链接认不出、view 元数据取不到这两件事才向外抛 BiliMaterialError。

    缓存：(bvid, 入参变体) → markdown，TTL 600 秒、上限 50 条；相同入参直接命中；
    不同入参（开关 / 条数 / 截断 / 分 P 页码 p= / 有无凭据 / asr 开关）各占一条、互不失效，
    两条链路交替调用也不会反复联网。

    Args:
        url: 视频链接 / BV 号 / av 号 / b23.tv 短链（短链自动先还原；元数据块的展示链接
            会收敛成规范链接——去掉分享参数、裸号补全）。
        include_danmaku: 要不要弹幕段。
        include_comments: 要不要热门评论段。
        include_timestamps: 字幕/弹幕要不要带 [MM:SS] 前缀。
        danmaku_limit: 弹幕条数上限（<= 0 视作没有弹幕）。
        comment_limit: 评论条数上限（<= 0 视作没有评论）。
        max_chars: 输出总字符上限（<= 0 视作不限幅）。
        credential: bilibili_api 的 Credential 或 None（匿名）。
        asr_cfg: 只接 [asr] 配置段（配置对象或同名字段的 dict；误传插件全量配置时
            读不到 enabled，等同不启用）。默认 None = 不做语音转写（与旧版行为一致）。
            只有字幕缺席（失败/无轨/正文为空）且本段 enabled 为真才尝试 ASR；
            enabled 为真但 provider/key 缺失，才写「语音转写未启用」的诊断提示。

    Returns:
        组装好的 Markdown 文本。

    Raises:
        BiliMaterialError: 链接无法识别（含短链还原后仍认不出）或 view 元数据获取失败。
    """
    text = str(url or "").strip()
    if not text:
        raise BiliMaterialError("无法从链接中识别 BV 号: 空链接")
    link = text
    try:
        bvid = extract_bvid(link)
    except BiliMaterialError:
        # 短链（或夹在分享文案里的短链）先还原终态 URL 再认一次；仍认不出就把原异常原样往外抛
        link = await resolve_b23_short(link)
        bvid = extract_bvid(link)

    # [asr] 开关位：缓存变体只看 enabled、不看 provider 解析结果——那是缓存未命中之后才做的功课
    asr_wanted = _asr_enabled_flag(asr_cfg)
    variant = _cache_variant(
        include_danmaku,
        include_comments,
        include_timestamps,
        danmaku_limit,
        comment_limit,
        max_chars,
        credential,
        _link_page(link),
        asr_wanted,
    )
    cached = _cache_get(bvid, variant)
    if cached is not None:
        logger.debug(f"素材缓存命中：{bvid}")
        return cached
    logger.debug(f"素材缓存未命中，开始抓取：{bvid}")

    view = await fetch_view(bvid)  # 失败就抛 BiliMaterialError——元数据是素材的地基，这段不许降级
    aid = _as_int(view.get("aid"))
    # 头部说明：内容依据的优先级——字幕 / 转写 > 观众反应
    blocks = [MATERIAL_HEADER_NOTE, "", format_meta_markdown(view, _canonical_link(link, bvid))]

    # ── 先把「字幕」请上桌 ──
    blocks.append("## 字幕全文")
    subtitle_ok = False
    try:
        ok, subtitle = await fetch_subtitle_text(bvid, credential, include_timestamps)
        if ok and subtitle:
            subtitle_ok = True
            blocks.append(subtitle)
        else:
            blocks.append("> （未获取到字幕，可能无 CC 字幕或需要登录）")
    except Exception as exc:  # noqa: BLE001
        blocks.append(f"> （字幕获取失败：{_exc_text(exc, credential)}）")
    blocks.append("")

    # ── 语音转写（ASR）：字幕缺席 且 [asr].enabled 为真，才轮到它补位 ──
    #（门槛与缓存 ar 位同语义：工具恒传 cfg.asr，但默认关时素材不多出这一段；
    #  「未启用」提示只留给 enabled=true 却缺 provider/key 的真诊断场景）
    page_no = _link_page(link)
    asr_delivered = False
    if asr_wanted and not subtitle_ok:
        blocks.append("## 语音转写（ASR）")
        settings = load_asr_settings(asr_cfg)
        if settings is None:
            blocks.append("> （语音转写未启用：缺少 api_base/api_key 且 provider 复用未拿到）")
        else:
            # 按目标分 P 的时长判上限（取法与 format_meta_markdown 同源）：多 P 视频不被
            # 全片总时长误伤；pages 取不到才回退全片时长，降级文案会分别标注
            page_duration = -1
            pages = view.get("pages")
            if isinstance(pages, list) and 1 <= page_no <= len(pages) and isinstance(pages[page_no - 1], dict):
                page_duration = _as_int(pages[page_no - 1].get("duration"), -1)
            if page_duration >= 0:
                limit_label, duration = f"P{page_no} 时长", page_duration
            else:
                limit_label, duration = "全片时长", _as_int(view.get("duration"))
            if settings.max_minute > 0 and duration > settings.max_minute * 60:
                blocks.append(
                    f"> （{limit_label} {_fmt_duration(duration)} 超过 [asr].max_minute="
                    f"{settings.max_minute} 分钟，跳过语音转写）"
                )
            else:
                try:
                    # 外层硬时限：下载没有单一请求级超时兜底、转写上传也可能慢，整段以
                    # timeout_seconds + 宽限封顶；到点只降级成一行说明，不抛
                    delivered, body = await asyncio.wait_for(
                        _run_asr_segment(bvid, page_no, asr_cfg, settings, credential),
                        timeout=_asr_outer_timeout(settings),
                    )
                    asr_delivered = delivered
                    blocks.append(body)
                except asyncio.TimeoutError:
                    timeout_text = _asr_outer_timeout(settings)
                    logger.warning(f"语音转写段超时（{bvid} P{page_no}）：超过 {timeout_text:.0f}s 总时限")
                    blocks.append(f"> （语音转写超时：超过 {timeout_text:.0f} 秒总时限，已跳过）")
        blocks.append("")

    # ── 字幕与转写双缺席的专用提示：提醒 LLM 别把观众反应当成视频内容 ──
    if not subtitle_ok and not asr_delivered:
        blocks.append(ASR_NO_EVIDENCE_NOTE)
        blocks.append("")

    # ── 弹幕上桌（是观众反应，不是视频内容哦） ──
    if include_danmaku:
        blocks.append("## 弹幕精选（观众反应，非视频内容）")
        try:
            danmakus = await fetch_danmakus(bvid, danmaku_limit)
            if danmakus:
                for danmaku in danmakus:
                    prefix = f"[{_fmt_ts(danmaku.get('dm_time', 0))}] " if include_timestamps else ""
                    blocks.append(f"{prefix}{danmaku.get('text', '')}")
            else:
                blocks.append("> （暂无弹幕）")
        except BiliDanmakuClosedError:
            blocks.append("> （弹幕已关闭）")
        except Exception as exc:  # noqa: BLE001
            blocks.append(f"> （弹幕获取失败：{_exc_text(exc, credential)}）")
        blocks.append("")

    # ── 热门评论登场（同样是观众反应，不是视频内容） ──
    if include_comments:
        blocks.append("## 热门评论（观众反应，非视频内容）")
        try:
            comments = await fetch_hot_comments(aid, comment_limit)
            if comments:
                for comment in comments:
                    uname = comment.get("uname") or "匿名"
                    blocks.append(
                        f"- **{uname}**（赞 {comment.get('like', 0)}）：{comment.get('message', '')}"
                    )
            else:
                blocks.append("> （暂无评论）")
        except Exception as exc:  # noqa: BLE001
            blocks.append(f"> （评论获取失败：{_exc_text(exc, credential)}）")
        blocks.append("")

    markdown = _truncate_markdown("\n".join(blocks), max_chars)
    _cache_put(bvid, markdown, variant)
    return markdown


# ---------------------------------------------------------------- 搜索输出


_SEARCH_TAG_RE = re.compile(r"<[^>]+>")
_SEARCH_SPACE_RE = re.compile(r"\s+")


def _clean_search_text(value: Any) -> str:
    """清理搜索结果文本：扒掉 HTML 高亮标签、还原实体、把换行压平。"""
    raw = _SEARCH_TAG_RE.sub("", str(value or ""))
    return _SEARCH_SPACE_RE.sub(" ", html.unescape(raw)).strip()


def _search_duration_text(value: Any) -> str:
    """搜索结果的时长：接口可能给 "MM:SS"/"H:MM:SS" 字符串，也可能给秒数，两种都接得住。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _fmt_duration(value)


def _extract_search_item(item: Any) -> dict | None:
    """从 search_by_type 的条目里挑出素材字段（本模块自己实现，不去翻 fetcher 的私有方法）。"""
    if not isinstance(item, dict):
        return None
    bvid = str(item.get("bvid") or "").strip()
    if not bvid:
        matched = _BV_RE.search(str(item.get("arcurl") or ""))
        bvid = matched.group(1) if matched else ""
    title = _clean_search_text(item.get("title"))
    if not title:
        return None
    description = _clean_search_text(item.get("description"))
    if len(description) > SEARCH_DESC_MAX_CHARS:
        description = description[: SEARCH_DESC_MAX_CHARS - 1].rstrip() + "…"
    return {
        "title": title,
        "bvid": bvid,
        "author": _clean_search_text(item.get("author")) or "匿名",
        "play": _fmt_count(item.get("play", 0)),
        "duration": _search_duration_text(item.get("duration")),
        "typename": _clean_search_text(item.get("typename")),
        "description": description,
        "url": SEARCH_VIDEO_URL.format(bvid=bvid) if bvid else str(item.get("arcurl") or ""),
    }


async def search_videos_markdown(query: str, limit: int) -> str:
    """搜索 B 站视频，输出一份 Markdown 清单。

    Args:
        query: 搜索关键词（空关键词直接回提示，不发起请求）。
        limit: 返回条数上限（<= 0 直接回提示；page_size 夹在 [1, 42]）。

    Returns:
        以「# B站搜索结果」开头的 Markdown：每条含 标题 / BV / UP 主 / 播放 / 时长 / 分区 / 简介 / 链接。

    Raises:
        BiliMaterialError: 搜索接口/网络异常。
    """
    keyword = str(query or "").strip()
    count = _as_int(limit)
    head = "# B站搜索结果"
    if not keyword:
        return f"{head}\n\n> （搜索关键词为空）"
    if count <= 0:
        return f"{head}\n\n> （搜索结果条数上限为 0，未发起搜索）"

    page_size = max(1, min(count, SEARCH_PAGE_SIZE_MAX))
    try:
        payload = await search_by_type(
            keyword, search_type=SearchObjectType.VIDEO, page_size=page_size
        )
    except Exception as exc:  # noqa: BLE001
        # 异常文本抛出前先脱敏：这条通路是匿名调用、手上没凭据，全靠登记册兜底
        raise BiliMaterialError(
            _redact(f"搜索失败（{keyword}）：{type(exc).__name__}: {exc}")
        ) from exc

    raw_items = payload.get("result") if isinstance(payload, dict) else None
    items: list[dict] = []
    for raw in raw_items or []:
        info = _extract_search_item(raw)
        if info is None:
            continue
        items.append(info)
        if len(items) >= count:
            break
    if not items:
        return f"{head}\n\n> （未搜索到「{keyword}」相关视频）"

    blocks = [head, "", f"关键词：{keyword}（{len(items)} 条）", ""]
    for index, info in enumerate(items, 1):
        blocks.append(f"{index}. **{info['title']}**")
        blocks.append(f"   - BV：{info['bvid'] or '（未识别）'}")
        blocks.append(f"   - UP 主：{info['author']}")
        blocks.append(f"   - 播放：{info['play']}")
        blocks.append(f"   - 时长：{info['duration']}")
        if info["typename"]:
            blocks.append(f"   - 分区：{info['typename']}")
        if info["description"]:
            blocks.append(f"   - 简介：{info['description']}")
        blocks.append(f"   - 链接：{info['url']}")
    return "\n".join(blocks)
