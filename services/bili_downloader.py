"""B 站视频自研下载服务——这位是「跑腿小哥」：B 站把视频和音频拆成两条 DASH 流，
我们分头下载，再请 ffmpeg 把它们拼回一个 mp4 递到聊天里。

对外符号：
- resolve_ffmpeg：三级找 ffmpeg 可执行文件（配置 ffmpeg_path → shutil.which 查系统 PATH →
  plugin_api.get_plugin_path("parser") + /bin/ffmpeg.exe）；三级都落空就返回 ""；
- download_bili_video：认链接 → 取分 P 的 DASH 双流 → 流式下载 → ffmpeg -c copy 合流；
  超限/失败一律抛 BiliDownloadError，并把半截文件收拾干净；
- download_bilibili_audio：只下 DASH 音频单轨（给 ASR 用：不合并、不下视频流），
  临时文件由调用方负责清理（cleanup_temp）；沿用体积预算与时长上限，失败同样抛契约异常；
- send_video_file：经 send_api.send_file 直发文件（不用 base64、不用合并转发），
  delete_after=False 时保留原文件（被动链路发 parser 缓存文件的情形）；
- _DOWNLOAD_SEMAPHORE：全局下载并发上限 2，推送和被动两条链路共用这一道门。

关键语义（都是实践里换来的经验，一条条写进规则）：
- max_size_mb <= 0 与 max_minute <= 0 都表示「不限制」；
- 体积预算按「视频流 + 音频流合并总大小」计：先下音频（个头小），再拿
  「总预算 − 音频已占」当视频流预算；任一流越界立即拒绝并清理半截文件，
  合流之后还要再做一次总大小兜底校验；
- 时长上限独立检查（在取播放地址之前）；
- 画质就近降级：请求的档位拿不到时，取 <= 请求档的最高可用流而不算失败；
  AVC 优先，实在没有 AVC 才回退其它编码。

链接识别直接复用 material.py 的 extract_bvid / resolve_b23_short / load_credential，
本模块不重复造轮子（回归断言用 is 比对确认是同一函数对象）。

MIT 归属：本模块为自研实现；「取 playurl → 挑流 → 流式下载 → ffmpeg -c copy 合流」的链路
形态参考 BiliLearn（xiaoyaya191/bilibili_learning_bot, © 2025 XingYe contributors, MIT）与
bilibili_api 公开文档。零 parser 代码复制（parser 为 GPL-3.0）：仅运行时经
plugin_api.get_plugin_path 复用其捆绑的 ffmpeg 二进制路径。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from bilibili_api.video import (
    AudioStreamDownloadURL,
    Video,
    VideoCodecs,
    VideoDownloadURLDataDetecter,
    VideoQuality,
    VideoStreamDownloadURL,
)

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.plugin_api import get_plugin_path
from src.app.plugin_system.api.send_api import send_file, send_video

from .material import (
    REFERER,
    UA,
    BiliMaterialError,
    _redact,  # 统一的脱敏入口：契约异常文本抛出去之前，先把凭据值擦干净
    extract_bvid,
    load_credential,
    resolve_b23_short,
)

logger = get_logger("bili_sharer_download")

# 本插件名（拿来定缓存目录）和 parser 插件名（借它捆绑的 ffmpeg）。
PLUGIN_NAME = "bilibili_sharer"
PARSER_PLUGIN_NAME = "parser"
FFMPEG_BIN_DIR = "bin"
FFMPEG_BIN_NAME = "ffmpeg.exe"
# 缓存目录：<仓库根>/data/bilibili_sharer/cache（自家地盘；发完即删，失败清半截）。
CACHE_RELATIVE_PARTS = ("data", "bilibili_sharer", "cache")

DEFAULT_QUALITY = "720P"
DOWNLOAD_TIMEOUT = 60.0
CONNECT_TIMEOUT = 15.0
MERGE_TIMEOUT = 120.0
# 单次 read 的分片大小（测试会临时调小它，好构造「半截文件」的场景）。
CHUNK_SIZE = 256 * 1024
ORIGIN = "https://www.bilibili.com"
# detect() 允许参与挑选的编码（分类顺序就是此列表顺序）；正式挑时再 AVC 优先。
ACCEPTED_CODECS = [VideoCodecs.AV1, VideoCodecs.AVC, VideoCodecs.HEV, VideoCodecs.UNKNOWN]

# 全局下载并发上限：推送与被动两条链路共用这一道门，最多 2 个下载同时在跑。
_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)


class BiliDownloadError(Exception):
    """B 站视频下载/合流失败的统一异常：链接认不出 / 接口错误 / 超体积超时长 / ffmpeg 不配合。"""


# ---------------------------------------------------------------- ffmpeg 解析


def resolve_ffmpeg(ffmpeg_path: str) -> str:
    """分三级、就近到远地找 ffmpeg 可执行文件。

    顺序：
    1. 配置 [video_send] ffmpeg_path（支持 %VAR% 展开 / 去引号；写成命令名就走 PATH）；
    2. shutil.which("ffmpeg")（系统 PATH）；
    3. plugin_api.get_plugin_path("parser") + /bin/ffmpeg.exe（借 parser 捆绑的那份）。

    三级都落空就返回 ""（由调用方给降级提示），本函数不抛——parser 缺席
    （get_plugin_path 返回 None 或抛错）只是少一级候选而已。

    Args:
        ffmpeg_path: 配置里写的 ffmpeg 路径（可以为空串）。

    Returns:
        可用的 ffmpeg 路径；三级都不行时是空串。
    """
    configured = os.path.expandvars(str(ffmpeg_path or "").strip().strip('"').strip("'"))
    if configured:
        if os.path.isfile(configured):
            return configured
        found = shutil.which(configured)
        if found:
            return found
        logger.warning(f"[video_send] 配置的 ffmpeg 路径不可用，继续回退：{configured}")

    found = shutil.which("ffmpeg")
    if found:
        return found

    try:
        parser_dir = get_plugin_path(PARSER_PLUGIN_NAME)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[video_send] 查询 parser 插件路径失败（忽略该级）：{type(exc).__name__}: {exc}")
        parser_dir = None
    if parser_dir:
        bundled = Path(parser_dir) / FFMPEG_BIN_DIR / FFMPEG_BIN_NAME
        if bundled.is_file():
            return str(bundled)
        logger.warning(f"[video_send] parser 目录内没有 {FFMPEG_BIN_DIR}/{FFMPEG_BIN_NAME}：{parser_dir}")

    logger.warning("[video_send] 三级都没找到 ffmpeg（配置路径 / 系统 PATH / parser 捆绑）")
    return ""


def _repo_root() -> Path:
    """仓库根目录：优先请 plugin_api 反推，推不出再按本文件位置回退。"""
    try:
        plugin_dir = get_plugin_path(PLUGIN_NAME)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[video_send] 查询插件自身路径失败，按文件位置回退：{type(exc).__name__}: {exc}")
        plugin_dir = None
    if plugin_dir:
        candidate = Path(plugin_dir).resolve()
        if candidate.name == PLUGIN_NAME and candidate.parent.name == "plugins":
            return candidate.parent.parent
    return Path(__file__).resolve().parents[3]


def _cache_dir() -> Path:
    """下载缓存目录（自动创建）：<仓库根>/data/bilibili_sharer/cache。"""
    directory = _repo_root().joinpath(*CACHE_RELATIVE_PARTS)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# ---------------------------------------------------------------- 配置读取（stub 友好）


def _video_send_section(cfg: Any) -> Any:
    """取 [video_send] 段：递来插件全量配置（带 video_send 属性）或只递这一段本身，都接得住。"""
    if cfg is None:
        return None
    section = cfg.get("video_send") if isinstance(cfg, dict) else getattr(cfg, "video_send", None)
    return section if section is not None else cfg


def _credential_section(cfg: Any) -> Any:
    """取 [bili_credential] 段；没有这段就当匿名访问。"""
    if cfg is None:
        return None
    if isinstance(cfg, dict):
        return cfg.get("bili_credential")
    return getattr(cfg, "bili_credential", None)


def _cfg_field(section: Any, name: str, default: Any) -> Any:
    """兼容 pydantic 配置对象与 dict 的取值；只有值是 None 才回退默认——0 是正经值，不能替它做主。"""
    if section is None:
        return default
    value = section.get(name, default) if isinstance(section, dict) else getattr(section, name, default)
    return default if value is None else value


def _budget_bytes(max_size_mb: Any) -> float:
    """体积上限换成字节：max_size_mb <= 0 或非法值时返回 inf（等于不设限）。"""
    try:
        megabytes = float(max_size_mb)
    except (TypeError, ValueError):
        logger.warning(f"[video_send] max_size_mb 非法（{max_size_mb!r}），按不限制处理")
        return float("inf")
    if megabytes != megabytes:  # NaN
        return float("inf")
    if megabytes <= 0:
        return float("inf")
    return megabytes * 1024 * 1024


def _duration_limit_seconds(max_minute: Any) -> float:
    """时长上限换算成秒：max_minute <= 0 或非法值时返回 inf（等于不设限）。"""
    try:
        minutes = float(max_minute)
    except (TypeError, ValueError):
        logger.warning(f"[video_send] max_minute 非法（{max_minute!r}），按不限制处理")
        return float("inf")
    if minutes != minutes:  # NaN
        return float("inf")
    if minutes <= 0:
        return float("inf")
    return minutes * 60.0


def _remaining_budget(total: float, used: float) -> float:
    """剩余预算 = 上限减已占；上限是 inf 的话，剩下的自然还是 inf。"""
    return total - used if total != float("inf") else float("inf")


def _fmt_budget(value: float) -> str:
    """预算的展示文本：日志和异常文案共用这一个口径。"""
    return "不限制" if value == float("inf") else f"{value:.0f}"


_QUALITY_ALIASES = {
    "1080P+": "_1080P_PLUS",
    "1080PPLUS": "_1080P_PLUS",
    "1080P60": "_1080P_60",
}


def _map_quality(raw: Any) -> VideoQuality:
    """画质名翻成 VideoQuality；认不出或没填就回退 720P（与配置说明一致）。"""
    text = str(raw or "").strip()
    if not text:
        return VideoQuality._720P
    compact = text.upper().replace(" ", "")
    name = _QUALITY_ALIASES.get(compact) or (compact if compact.startswith("_") else "_" + compact)
    quality = getattr(VideoQuality, name, None)
    if isinstance(quality, VideoQuality):
        return quality
    logger.warning(f"[video_send] 非法画质配置 {raw!r}，回退 {DEFAULT_QUALITY}")
    return VideoQuality._720P


# ---------------------------------------------------------------- 页码解析


def _page_index(page_num: Any) -> int:
    """1 基页码换成 0 基索引（<=1 或非法一律当第 0 集）。"""
    try:
        value = int(page_num)
    except (TypeError, ValueError):
        return 0
    return value - 1 if value > 1 else 0


def _url_page(url: Any) -> int:
    """从链接 query 里正规地解析分 P 页码 p=（缺失/非法/<=1 一律按 1）。

    这是个教训：p= 不保证是链接的第一个参数，必须 urlparse + parse_qs 好好翻一遍，
    不能拿正则赌它紧跟在 bvid 后面。
    """
    text = str(url or "")
    if "://" not in text:
        return 1
    page = (parse_qs(urlparse(text).query).get("p") or [""])[0]
    return int(page) if str(page).isdigit() and int(page) > 1 else 1


def _effective_page(page_num: Any, url: str) -> int:
    """显式 page_num 优先；没显式指定（<=1）时，就信链接里带的 ?p=。"""
    if _page_index(page_num) > 0:
        return _page_index(page_num) + 1
    return _url_page(url)


# ---------------------------------------------------------------- 选流


def _pick_video_stream(streams: list, target: VideoQuality) -> VideoStreamDownloadURL | None:
    """挑视频流：AVC 优先、同档取最高；完全没有 AVC 才退让给其它编码；只收 <= 请求档且非 HDR/杜比的。"""
    candidates = [
        stream
        for stream in streams or []
        if isinstance(stream, VideoStreamDownloadURL)
        and stream.video_quality not in (VideoQuality.HDR, VideoQuality.DOLBY)
        and stream.video_quality.value <= target.value
    ]
    if not candidates:
        return None
    avc = [stream for stream in candidates if stream.video_codecs == VideoCodecs.AVC]
    return max(avc or candidates, key=lambda stream: stream.video_quality.value)


def _pick_audio_stream(streams: list) -> AudioStreamDownloadURL | None:
    """挑音频流：在所有可用档里拿清晰度最高的那条。"""
    candidates = [stream for stream in streams or [] if isinstance(stream, AudioStreamDownloadURL)]
    if not candidates:
        return None
    return max(candidates, key=lambda stream: stream.audio_quality.value)


def _request_headers() -> dict[str, str]:
    """CDN 请求头：UA + Referer + Origin（本机验证过够用的最小组合）。

    刻意不带 Cookie：playurl 给出的流地址自带鉴权参数，实在没必要把 SESSDATA
    寄往第三方 CDN 的域名。
    """
    return {"User-Agent": UA, "Referer": REFERER, "Origin": ORIGIN}


def _unlink(path: Any) -> None:
    """安全删文件：文件不存在、删除失败，都不让它抛出惊吓。"""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(f"[video_send] 删除文件失败：{path} -> {exc}")


# ---------------------------------------------------------------- 下载 / 合流


async def _fetch_page(video: Video, page_num: int) -> dict:
    """取指定分 P 的信息（cid / duration / part）；页码越界或接口失败抛 BiliDownloadError。"""
    try:
        pages = await video.get_pages()
    except Exception as exc:  # noqa: BLE001
        raise BiliDownloadError(f"获取分 P 信息失败：{type(exc).__name__}: {exc}") from exc
    pages = pages or []
    if not pages:
        raise BiliDownloadError("该视频没有可用的分 P 信息")
    index = _page_index(page_num)
    if index >= len(pages):
        raise BiliDownloadError(f"分 P 页码 {page_num} 超出视频总 P 数 {len(pages)}")
    page = pages[index]
    return page if isinstance(page, dict) else {}


async def _select_streams(video: Video, cid: int, target: VideoQuality) -> tuple[Any, Any]:
    """取 DASH 音视频双流：画质档位就近降级 + AVC 优先。

    detect 撞见播放地址缺字段（KeyError）或别的脏结构（TypeError/ValueError 等）时，
    一律包装成契约异常 BiliDownloadError，不裸抛——与音频路径同一手法。
    """
    try:
        data = await video.get_download_url(cid=cid)
    except Exception as exc:  # noqa: BLE001
        raise BiliDownloadError(f"获取播放地址失败：{type(exc).__name__}: {exc}") from exc
    detector = VideoDownloadURLDataDetecter(data or {})
    if not detector.check_video_and_audio_stream():
        raise BiliDownloadError("该视频未提供 DASH 音视频分离流，自研下载暂不支持")
    # 与音频路径 download_bilibili_audio 同一手法：detect 撞见播放地址结构缺字段
    # 会裸抛 KeyError，其它脏结构也可能 TypeError/ValueError——一律包装为契约异常
    try:
        streams = detector.detect(
            video_max_quality=target,
            codecs=ACCEPTED_CODECS,
            no_hdr=True,
            no_dolby_video=True,
            no_dolby_audio=True,
            no_hires=True,
        )
    except KeyError as exc:
        raise BiliDownloadError(f"播放地址结构缺少视频/音频必需字段：{exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise BiliDownloadError(f"解析播放地址失败：{type(exc).__name__}: {exc}") from exc
    video_stream = _pick_video_stream(streams, target)
    audio_stream = _pick_audio_stream(streams)
    if video_stream is None:
        raise BiliDownloadError(f"没有 <= {target.name.lstrip('_')} 的可用视频流")
    if audio_stream is None:
        raise BiliDownloadError("没有可用音频流")
    if video_stream.video_quality.value < target.value:
        logger.info(
            f"[video_send] 请求画质 {target.name.lstrip('_')} 不可得，就近降级到 "
            f"{video_stream.video_quality.name.lstrip('_')}（编码 {video_stream.codecs}）"
        )
    return video_stream, audio_stream


async def _stream_to_file(client: httpx.AsyncClient, url: str, dest: Path, *, max_bytes: float, label: str) -> int:
    """流式下载单条流到 dest；越限/异常一律删掉 dest（半截清理）并抛 BiliDownloadError。

    Args:
        client: 复用的 httpx 客户端。
        url: 流地址。
        dest: 分片落盘路径。
        max_bytes: 本次可用的预算（字节，可以是 inf）。
        label: 日志/异常里的称呼（音频流 / 视频流）。

    Returns:
        实际写入的字节数。

    Raises:
        BiliDownloadError: HTTP 错误 / 空内容 / 下载不完整 / 超过预算；
            httpx 传输层异常和磁盘 OSError 也统一包装成它，不放裸异常出门。
    """
    downloaded = 0
    declared: int | None = None
    try:
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise BiliDownloadError(f"{label} 下载失败：HTTP {response.status_code}")
            raw_length = response.headers.get("content-length")
            declared = int(raw_length) if raw_length and str(raw_length).isdigit() else None
            if declared is not None and declared > max_bytes:
                raise BiliDownloadError(
                    f"{label} 内容长度 {declared} 字节超过可用预算 {_fmt_budget(max_bytes)}"
                )
            with open(dest, "wb") as fp:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise BiliDownloadError(
                            f"{label} 已下 {downloaded} 字节超过可用预算 {_fmt_budget(max_bytes)}"
                        )
                    fp.write(chunk)
        if downloaded == 0:
            raise BiliDownloadError(f"{label} 下载内容为空")
        if declared is not None and downloaded < declared:
            raise BiliDownloadError(f"{label} 下载不完整（{downloaded}/{declared} 字节）")
        return downloaded
    except (httpx.HTTPError, OSError) as exc:
        # 传输层意外（连接重置/读超时/响应体截断）与磁盘异常同样先清半截文件，
        # 再包装成契约异常——调用方只需 catch BiliDownloadError，不会被网络抖动炸穿链路。
        _unlink(dest)
        raise BiliDownloadError(f"{label} 流式下载失败：{type(exc).__name__}: {exc}") from exc
    except BaseException:
        _unlink(dest)  # 其余异常（含本函数自己抛的 BiliDownloadError）：清完现场、原样上抛
        raise


async def _merge_av(
    ffmpeg: str,
    video_path: Path,
    audio_path: Path,
    out_path: Path,
    *,
    timeout: float = MERGE_TIMEOUT,
) -> float:
    """用 ffmpeg -c copy 把音视频拼起来（不重新编码），返回耗时秒数。"""
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-c",
        "copy",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        str(out_path),
    ]
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        raise BiliDownloadError(f"ffmpeg 无法执行（{ffmpeg}）：{type(exc).__name__}: {exc}") from exc
    try:
        _out, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise BiliDownloadError(f"ffmpeg 合流超时（{timeout:.0f}s）") from exc
    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", errors="ignore").strip()[-300:]
        raise BiliDownloadError(f"ffmpeg 合流失败（exit={process.returncode}）：{detail}")
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise BiliDownloadError("ffmpeg 合流未产出有效文件")
    return time.monotonic() - started


def _build_video(bvid: str, credential: Any) -> Video:
    """按视频号类型构造 Video。

    真实库的 Video(bvid=...) 强制校验 `^BV[a-zA-Z0-9]{10}$`（set_bvid）：av 号原样塞进
    bvid 必抛裸 ArgsException，所以 av 必须走 Video(aid=int)；构造异常一律包装成契约异常。
    """
    text = str(bvid)
    try:
        if text[:2].lower() == "av" and text[2:].isdigit():
            return Video(aid=int(text[2:]), credential=credential)
        return Video(bvid=text, credential=credential)
    except Exception as exc:  # noqa: BLE001
        raise BiliDownloadError(f"构造 Video 失败：{type(exc).__name__}: {exc}") from exc


def _page_duration_seconds(page_info: Any, page: int) -> float:
    """分 P 时长的安全转换：绝不裸抛 ValueError/TypeError。

    duration 是「时长上限」检查的唯一依据——脏值按 0 放行，就会误下载超长的视频，
    所以这里宁可明确失败：解析不了就包装成契约异常（与「超限即弃」同一家族的脾气）。
    """
    raw = page_info.get("duration") if isinstance(page_info, dict) else None
    try:
        return float(raw or 0)
    except (TypeError, ValueError) as exc:
        raise BiliDownloadError(f"分 P {page} 时长字段不可解析：{raw!r}") from exc


def _page_cid(page_info: Any, page: int) -> int:
    """分 P cid 的安全取整：脏 cid 同样不许裸抛 ValueError，一律走契约异常。"""
    raw = page_info.get("cid") if isinstance(page_info, dict) else None
    try:
        return int(raw or 0)
    except (TypeError, ValueError) as exc:
        raise BiliDownloadError(f"分 P {page} 的 cid 不可解析：{raw!r}") from exc


async def download_bili_video(url_or_bvid: str, *, page_num: int = 1, cfg: Any) -> tuple[Path, dict]:
    """下载并合流一个 B 站视频的指定分 P。

    链路：extract_bvid / resolve_b23_short（复用 material）→ 按号型构造 Video
    （BV→bvid、av→aid；credential 可选）→ 取分 P（cid/duration）→ 时长检查 →
    取 DASH 双流（画质就近降级、AVC 优先）→ 先下音频、再按剩余预算下视频 →
    ffmpeg 合流（token 命名）→ os.replace 换回稳定名（稳定名被并发发送占用时，
    保持 token 名交付）→ 清理分片 → 返回 (产物路径, 元数据)。

    Args:
        url_or_bvid: 视频链接 / BV 号 / av 号 / b23.tv 短链（带 ?p= 时自动取页码）。
        page_num: 分 P 页码（1 基）。<=1 且链接带 ?p=N 时按链接的页码。
        cfg: 插件配置对象（读 cfg.video_send.* 与 cfg.bili_credential）；也接受
            只递 [video_send] 段本身（此时按匿名访问）。

    Returns:
        (产物 mp4 路径, 元数据 dict)——正常是稳定名 {bvid}_p{page}.mp4，只有稳定名被
        并发发送占用时才交付 token 名的同内容产物；元数据含 bvid/page/cid/duration/画质/
        各流字节数/产物大小/预算/ffmpeg/合流耗时/降级标记等。

    Raises:
        BiliDownloadError: 链接认不出 / 没 ffmpeg / Video 构造失败 / 分 P 越界 /
            超时长 / 超预算 / 非 DASH 流 / 传输层或磁盘异常 / 合流失败 / 凭据加载失败 /
            分 P 时长或 cid 脏值 / 播放地址结构异常（脏值统统契约收口，不放裸抛）。
    """
    text = str(url_or_bvid or "").strip()
    if not text:
        raise BiliDownloadError("链接为空，无法下载")

    section = _video_send_section(cfg)
    quality = _map_quality(_cfg_field(section, "video_quality", DEFAULT_QUALITY))
    budget = _budget_bytes(_cfg_field(section, "max_size_mb", 200))
    duration_limit = _duration_limit_seconds(_cfg_field(section, "max_minute", 30))
    ffmpeg = resolve_ffmpeg(str(_cfg_field(section, "ffmpeg_path", "") or ""))
    if not ffmpeg:
        raise BiliDownloadError(
            "未找到 ffmpeg：请在 [video_send] 配置 ffmpeg_path，或把 ffmpeg 放进系统 PATH，"
            "或启用 parser 插件（运行时复用其捆绑 plugins/parser/bin/ffmpeg.exe）"
        )
    # 凭据加载也护进契约包装：Credential 构造失手同样抛 BiliDownloadError，
    # 文本再过一遍 _redact（配置段里的字段值也在兜底范围内）。
    cred_cfg = _credential_section(cfg)
    try:
        credential = load_credential(cred_cfg)
    except Exception as exc:  # noqa: BLE001
        raise BiliDownloadError(
            f"凭据加载失败：{_redact(f'{type(exc).__name__}: {exc}', cred_cfg)}"
        ) from exc

    # 链接识别：直接借 material 家的 extract_bvid / resolve_b23_short，不再造一遍轮子
    resolved_url = text
    try:
        bvid = extract_bvid(text)
    except BiliMaterialError:
        resolved_url = await resolve_b23_short(text)
        try:
            bvid = extract_bvid(resolved_url)
        except BiliMaterialError as exc:
            raise BiliDownloadError(f"无法识别 B 站视频号：{text}") from exc

    page = _effective_page(page_num, resolved_url)
    if _page_index(page_num) == 0:
        page = max(page, _url_page(text))
    page_index = _page_index(page)

    async with _DOWNLOAD_SEMAPHORE:
        video = _build_video(bvid, credential)  # BV 走 bvid、av 走 aid；构造失败已包装成契约异常
        page_info = await _fetch_page(video, page)
        duration = _page_duration_seconds(page_info, page)  # 安全转换：脏时长值不裸抛
        if duration > duration_limit:
            raise BiliDownloadError(
                f"视频时长 {duration:.0f}s 超过上限 {duration_limit:.0f}s"
                f"（[video_send] max_minute={_cfg_field(section, 'max_minute', 30)}，<=0 表示不限制）"
            )
        cid = _page_cid(page_info, page)  # 安全取整：脏 cid 不裸抛
        video_stream, audio_stream = await _select_streams(video, cid, quality)

        try:
            directory = _cache_dir()  # mkdir 撞上磁盘问题也走契约异常，不放裸 OSError 出门
        except OSError as exc:
            raise BiliDownloadError(f"缓存目录准备失败：{type(exc).__name__}: {exc}") from exc
        token = uuid.uuid4().hex[:8]
        video_tmp = directory / f"{bvid}_p{page}_{token}_video.m4s"
        audio_tmp = directory / f"{bvid}_p{page}_{token}_audio.m4s"
        merged_tmp = directory / f"{bvid}_p{page}_{token}.mp4"  # 合流件同样带 token：绝不跟别的任务同名互踩
        merged = directory / f"{bvid}_p{page}.mp4"  # 稳定名（收件人能看到的文件名）；只有 os.replace 成功才由它接管
        _unlink(video_tmp)
        _unlink(audio_tmp)
        # 不预删稳定名 merged：同一 bvid+P 并发下载（Semaphore(2)）时，它可能正是
        # 另一个任务在发送的产物——抢先删它就是互踩。

        started = time.monotonic()
        final_path = merged_tmp  # 本任务的产物路径；改名成功后才切换成稳定名
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(DOWNLOAD_TIMEOUT, connect=CONNECT_TIMEOUT),
                headers=_request_headers(),
                follow_redirects=True,
            ) as client:
                # 先下音频（个头小），再把「总预算 − 音频已占」留给视频流当预算
                audio_bytes = await _stream_to_file(
                    client, audio_stream.url, audio_tmp, max_bytes=budget, label="音频流"
                )
                remaining = _remaining_budget(budget, audio_bytes)
                if remaining <= 0:
                    raise BiliDownloadError(
                        f"音频流已占满总预算（{audio_bytes}/{_fmt_budget(budget)} 字节）"
                    )
                video_bytes = await _stream_to_file(
                    client, video_stream.url, video_tmp, max_bytes=remaining, label="视频流"
                )
            merge_seconds = await _merge_av(ffmpeg, video_tmp, audio_tmp, merged_tmp)
            try:
                merged_size = merged_tmp.stat().st_size  # stat 撞上空合流件也照样包装（外层清理边界此时已生效）
            except OSError as exc:
                raise BiliDownloadError(f"合流产物不可用：{type(exc).__name__}: {exc}") from exc
            if merged_size > budget:  # 兜底校验：分片预算都算对时，这里不该触发
                raise BiliDownloadError(
                    f"合并后大小 {merged_size} 字节超过预算 {_fmt_budget(budget)}"
                )
            try:
                os.replace(merged_tmp, merged)  # 合流成功后原子接管稳定名——保住收件人看到的好文件名
                final_path = merged
            except OSError as exc:
                # 稳定名被并发任务发送占用（Windows 共享锁）：改用 token 名交付，绝不动别人的文件
                logger.warning(
                    f"[video_send] 稳定名不可用（{type(exc).__name__}: {exc}），改用临时名交付：{merged_tmp.name}"
                )
            # meta 构造与日志也放进清理边界：意外异常（比如 scale 缺失）也不会漏删产物
            actual = video_stream.video_quality
            meta = {
                "bvid": bvid,
                "page_num": page,
                "page_index": page_index,
                "cid": cid,
                "title": str(page_info.get("part") or ""),
                "duration": duration,
                "quality_requested": quality.name.lstrip("_"),
                "quality": actual.name.lstrip("_"),
                "quality_value": actual.value,
                "quality_downgraded": actual.value < quality.value,
                "codecs": str(video_stream.codecs or ""),
                "audio_quality": audio_stream.audio_quality.name.lstrip("_"),
                "width": int(video_stream.scale[0]),
                "height": int(video_stream.scale[1]),
                "video_bytes": int(video_bytes),
                "audio_bytes": int(audio_bytes),
                "size": int(merged_size),
                "budget_bytes": None if budget == float("inf") else int(budget),
                "duration_limit": None if duration_limit == float("inf") else float(duration_limit),
                "ffmpeg": ffmpeg,
                "merge_seconds": round(merge_seconds, 3),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "source_url": text,
                "resolved_url": resolved_url,
                "credential": credential is not None,
                "stable_name_taken": final_path is not merged,  # True = 稳定名被并发发送占用，本次交付的是 token 名产物
            }
            logger.info(
                f"[video_send] 下载完成 {bvid} P{page}：画质 {meta['quality']}"
                f"{'（已降级）' if meta['quality_downgraded'] else ''}，"
                f"视频 {video_bytes} + 音频 {audio_bytes} = {merged_size} 字节，"
                f"合流 {merge_seconds:.2f}s → {final_path}"
            )
            return final_path, meta
        except BaseException:
            _unlink(video_tmp)
            _unlink(audio_tmp)
            _unlink(final_path)  # 只清本任务自己的产物：token 名合流件，或改名成功后的稳定名件
            raise
        finally:
            _unlink(video_tmp)  # 合流成功，分片功成身退
            _unlink(audio_tmp)


# 「没显式传凭据」的哨兵：分清「调用方没传（内部按 cfg 读）」和「故意传 None（匿名）」。
_UNSET_CREDENTIAL: Any = object()


async def download_bilibili_audio(
    url_or_bvid: str, *, page_num: int = 1, cfg: Any, credential: Any = _UNSET_CREDENTIAL
) -> tuple[Path, dict]:
    """只下载一条 B 站视频的**音频流**（不合并、不下视频流；给 ASR 用，省流量省时间）。

    链路：extract_bvid / resolve_b23_short（复用 material）→ _build_video（BV→bvid、
    av→aid）→ 取分 P（cid/duration）→ 时长检查 → 取 DASH 并只挑音频流（排除杜比全景/
    Hi-Res，取最高可用 192K 档）→ 流式下载单轨产物 {bvid}_p{page}_{token}.m4a
    （token 名防并发互踩）。体积预算与时长上限沿用老读法：max_size_mb / max_minute
    从传入的段里读（传 [asr] 段时 max_minute 生效、max_size_mb 缺省 200MB）。

    **产物不自动删除**：清理归调用方（ASR 用完再删，请用 cleanup_temp）；失败路径
    清完半截文件再抛 BiliDownloadError，与 download_bili_video 同一契约；与视频下载
    共用 _DOWNLOAD_SEMAPHORE 这道门（全局并发上限 2）。

    Args:
        url_or_bvid: 视频链接 / BV 号 / av 号 / b23.tv 短链（带 ?p= 时自动取页码）。
        page_num: 分 P 页码（1 基）。<=1 且链接带 ?p=N 时按链接的页码。
        cfg: 配置对象；接受插件配置（读 cfg.video_send.*）、[video_send] 段或 [asr] 段
            本身（_video_send_section 同款宽容取段：没有 video_send 属性时，把传入段
            当作字段来源）。没显式传 credential 时从 cfg.bili_credential 读取（缺失 = 匿名）。
        credential: 已构造好的 bilibili_api Credential 或 None：material 会透传已加载的
            凭据，会员/登录可见视频的音频才可能取到；显式传 None = 匿名；不传
            （哨兵默认值）= 按 cfg.bili_credential 内部读取（保持原有行为）。

    Returns:
        (音频单轨路径, 元数据 dict)——元数据含 bvid/page/cid/duration/音频清晰度/字节数/
        预算/耗时/链接等（不含任何凭据值）。

    Raises:
        BiliDownloadError: 链接为空或认不出 / Video 构造失败 / 分 P 越界 / 超时长上限 /
            非 DASH 或没有可用音频流 / 超预算 / 传输层或磁盘异常 / 凭据加载失败 /
            分 P 时长或 cid 脏值（脏值统统契约收口）。
    """
    text = str(url_or_bvid or "").strip()
    if not text:
        raise BiliDownloadError("链接为空，无法下载音频")

    section = _video_send_section(cfg)
    budget = _budget_bytes(_cfg_field(section, "max_size_mb", 200))
    duration_limit = _duration_limit_seconds(_cfg_field(section, "max_minute", 10))
    if credential is _UNSET_CREDENTIAL:  # 只有没显式传入时才按 cfg 读取（加载失手会包装成契约异常）
        cred_cfg = _credential_section(cfg)
        try:
            credential = load_credential(cred_cfg)
        except Exception as exc:  # noqa: BLE001
            raise BiliDownloadError(
                f"凭据加载失败：{_redact(f'{type(exc).__name__}: {exc}', cred_cfg)}"
            ) from exc

    resolved_url = text
    try:
        bvid = extract_bvid(text)
    except BiliMaterialError:
        resolved_url = await resolve_b23_short(text)
        try:
            bvid = extract_bvid(resolved_url)
        except BiliMaterialError as exc:
            raise BiliDownloadError(f"无法识别 B 站视频号：{text}") from exc

    page = _effective_page(page_num, resolved_url)
    if _page_index(page_num) == 0:
        page = max(page, _url_page(text))
    page_index = _page_index(page)

    async with _DOWNLOAD_SEMAPHORE:
        video = _build_video(bvid, credential)
        page_info = await _fetch_page(video, page)
        duration = _page_duration_seconds(page_info, page)  # 安全转换：脏时长值不裸抛
        if duration > duration_limit:
            raise BiliDownloadError(
                f"P{page} 时长 {duration:.0f}s 超过音频下载上限 {duration_limit:.0f}s"
                f"（max_minute={_cfg_field(section, 'max_minute', 10)}，<=0 表示不限制）"
            )
        cid = _page_cid(page_info, page)  # 安全取整：脏 cid 不裸抛

        # 只挑音频流：不请求视频档位，排除杜比/Hi-Res（detect 默认封顶 192K）
        try:
            data = await video.get_download_url(cid=cid)
        except Exception as exc:  # noqa: BLE001
            raise BiliDownloadError(f"获取播放地址失败：{type(exc).__name__}: {exc}") from exc
        detector = VideoDownloadURLDataDetecter(data or {})
        if not detector.check_video_and_audio_stream():
            raise BiliDownloadError("该视频未提供 DASH 分离流，音频下载暂不支持")
        try:
            streams = detector.detect(no_dolby_audio=True, no_hires=True)
        except KeyError as exc:
            raise BiliDownloadError(f"播放地址结构缺少音频必需字段：{exc}") from exc
        except Exception as exc:  # noqa: BLE001
            # 与视频路径 _select_streams 同一手法：脏结构的杂七杂八异常也不裸抛
            # （此刻还没落盘任何文件，不需要半截清理；契约收口就是全部）。
            raise BiliDownloadError(f"解析播放地址失败：{type(exc).__name__}: {exc}") from exc
        audio_stream = _pick_audio_stream(streams)
        if audio_stream is None:
            raise BiliDownloadError("没有可用音频流")

        try:
            directory = _cache_dir()
        except OSError as exc:
            raise BiliDownloadError(f"缓存目录准备失败：{type(exc).__name__}: {exc}") from exc
        token = uuid.uuid4().hex[:8]
        audio_tmp = directory / f"{bvid}_p{page}_{token}.m4a"
        _unlink(audio_tmp)

        started = time.monotonic()
        try:
            # meta 构造与日志同样纳入清理边界：下载成功后哪怕出意外
            # （比如 meta 取值失手），也不会把音频产物漏在半路。
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(DOWNLOAD_TIMEOUT, connect=CONNECT_TIMEOUT),
                headers=_request_headers(),
                follow_redirects=True,
            ) as client:
                audio_bytes = await _stream_to_file(
                    client, audio_stream.url, audio_tmp, max_bytes=budget, label="音频流"
                )
            meta = {
                "bvid": bvid,
                "page_num": page,
                "page_index": page_index,
                "cid": cid,
                "title": str(page_info.get("part") or ""),
                "duration": duration,
                "audio_quality": audio_stream.audio_quality.name.lstrip("_"),
                "audio_bytes": int(audio_bytes),
                "size": int(audio_bytes),
                "budget_bytes": None if budget == float("inf") else int(budget),
                "duration_limit": None if duration_limit == float("inf") else float(duration_limit),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "source_url": text,
                "resolved_url": resolved_url,
                "credential": credential is not None,
            }
            logger.info(
                f"[asr] 音频单轨下载完成 {bvid} P{page}：{audio_stream.audio_quality.name.lstrip('_')} "
                f"{audio_bytes} 字节，耗时 {meta['elapsed_seconds']}s → {audio_tmp}"
            )
            return audio_tmp, meta
        except BaseException:
            _unlink(audio_tmp)  # 半截清理：若 _stream_to_file 内部已删过，这里幂等无害
            raise


def cleanup_temp(path: Any) -> None:
    """删除 download_bilibili_audio 留下的临时产物（复用 _unlink：不存在、失败都不抛）。"""
    _unlink(path)


# 缓存目录「孤儿文件」的 TTL：超过这个年纪没人碰的文件，视为不再有任务要用。
CACHE_ORPHAN_TTL_SECONDS = 6 * 3600.0


def cleanup_orphan_cache(max_age_seconds: float = CACHE_ORPHAN_TTL_SECONDS) -> int:
    """清理本插件缓存目录里超过 max_age_seconds 的孤儿文件，返回删掉的个数。

    背景：进程被中途杀掉、或清理路径失手时，data/bilibili_sharer/cache 会剩下半截
    分片/合流件。安全边界说在前头：
    - 只扫本插件缓存目录的顶层——不递归、不碰目录里的任何子目录、也不碰目录外的文件；
    - 只删「mtime 已超过 TTL」的普通文件——下载/发送在用的文件都是刚写的，按 mtime
      判断就会自然绕开在途产物；
    - 任何 OSError 只 log 不抛（启动清扫尽力而为，绝不影响插件加载）。
    """
    try:
        directory = _cache_dir()
        cutoff = time.time() - max(0.0, float(max_age_seconds))
        entries = list(directory.iterdir())
    except (OSError, ValueError) as exc:  # noqa: BLE001
        logger.warning(
            f"[video_send] 缓存目录孤儿清扫跳过（目录不可读）：{type(exc).__name__}: {exc}"
        )
        return 0
    removed = 0
    for item in entries:
        try:
            if not item.is_file():
                continue  # 只删普通文件，子目录一概不动
            if item.stat().st_mtime >= cutoff:
                continue  # 刚写的文件可能正被使用，跳过
        except OSError:
            continue
        _unlink(item)  # 幂等：失败自带 warning，不抛
        removed += 1
    if removed:
        logger.info(
            f"[video_send] 缓存目录启动清扫：删除 {removed} 个超过 "
            f"{max(0.0, float(max_age_seconds)) / 3600.0:.1f}h 的孤儿文件"
        )
    return removed


async def send_video_file(path: Path, stream_id: str, platform: str, *, delete_after: bool = True) -> bool:
    """经 send_api 发视频：优先 `send_video`（video 段，适配器通用），失败再回退 `send_file`（file 段，老适配器兜底）。

    Args:
        path: 要发送的本地视频路径。
        stream_id: 聊天流 ID。
        platform: 目标平台（适配器名）。
        delete_after: True（默认）= 发完就删（本插件自下载的产物，发完就没用了）；
            False = 发完保留（被动链路发 parser 缓存文件时用——删了会破坏 parser 的
            缓存语义）。

    Returns:
        是否发送成功；文件不存在或发送出意外时返回 False（不把异常往上扔）。
    """
    target = Path(path)
    if not target.is_file():
        logger.warning(f"[video_send] 待发送视频不存在：{target}")
        return False
    # 先按「视频段」发——这才是各适配器都认识的正规做法。
    # （旧实现用 send_file 发的是 file 段，OneBot 的 send_group_msg 会直接回 retcode 100）
    success = False
    try:
        success = bool(await send_video(target.as_uri(), stream_id, platform=platform))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[video_send] 视频段发送异常，准备回退 file 段：{type(exc).__name__}: {exc}")
        success = False
    if not success:
        # 回退：个别适配器 / 老版本只认 file 段（把文件当附件发）
        try:
            success = bool(
                await send_file(
                    str(target), stream_id=stream_id, platform=platform, file_name=target.name
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[video_send] file 段回退也失败：{type(exc).__name__}: {exc}")
            success = False
    # 收尾：发完按开关清理（不管成功与否，失败时留着也没用）
    if delete_after:
        _unlink(target)
    logger.info(
        f"[video_send] 视频{'已发送' if success else '发送失败'}"
        f"{'（本地文件已清理）' if delete_after else '（保留原文件）'}：{target.name} -> {stream_id}"
    )
    return success
