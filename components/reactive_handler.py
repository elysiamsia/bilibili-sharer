"""被动链路事件处理器 —— 群友丢来链接时，按开关替他把视频发出来，或顺口点评一句。

先说清楚：它有两个开关，默认都是关着的哟。

我是守在群消息旁边的那位：订阅 EventType.ON_MESSAGE_RECEIVED，来一条就看一眼。
处理顺序是排好的，不许乱：[reactive_filter] 白/黑名单 → 防自触发（三保险）→
链接识别（纯本地判断，不发网络请求、也不去问 parser）→ 按开关动手。
非 B 站链接归谁处理，要等到发视频那条分支才按 parser 的关键词判定。

发视频（video_send.reactive_enabled）：先判「避让五合一」——parser 已加载、
拿得到实例、search_key_pattern 命中、parser_map 认领该关键词、它的 is_session_allowed
也放行。五条同时成立就跳过发视频并记日志（它会发的，我不抢镜头）。

其中「认领」那条是防御性检查：现在的 parser 只要命中关键词就一定认领，所以这一条
眼下不会翻转任何判定；将来若它允许「模式命中但平台禁用」，本插件就会自动接管。

不让路时：B 站链接走自研下载（services/bili_downloader）；其他平台借道 parser，
走 parse → video_contents[0] → await get_path()，共享它的缓存文件后再发出去。
这一支里还有一层资源防抖：stream_id + 资源 ID，过期条目定期清掉。

自动点评（opinion.reactive_auto）：先过会话级频率限制（免得我话太多刷屏），
B 站取素材 + LLM 生成一句看法；其他平台默认跳过——本机没有「不触发下载」的
纯元信息通路。只有同一条消息已经因发视频被 parse 过时，才顺手复用它的
header/title/text/extra.info 写个简评。

语音转写是这样接进来的：点评分支总是把 [asr] 段交给素材服务（和聊天工具用同一把尺子），
到底要不要真的下载音频做转写，由 services/material.py 按 [asr].enabled 判定。
素材外层的兜底时限（MATERIAL_TIMEOUT 是基础值）跟着自适应放宽为
「基础值 + asr.timeout_seconds + 60s 宽限」；开关关着、或旧配置里根本没有 asr 段时，
时限和降级日志都和从前一字不差。

几个关键设计，说给想知道「为什么这样写」的你听：
- 始终注册：要不要动作全在 handler 内部判，不做条件注册（include 与 get_components 一致）。
- 后台处理：execute 里 create_task 之后立刻返回 SUCCESS，几十秒的长活儿不卡事件流超时。
- 不接管事件流：本处理器永远不返回 STOP（parser weight=50、聊天分发 priority=0，
  返回 STOP 会把消息挡在聊天流外面，那等于打断 bot 和大家聊天，可不行）。
- 并发与预算：两条发视频分支都过全局 _DOWNLOAD_SEMAPHORE（上限 2，与推送链路共用一个小门）；
  自研产物 delete_after=True（发完就删），parser 的缓存产物 delete_after=False——
  那些文件归 parser 的 CacheCleaner 管，我一律不删、也不动别人的东西。
- defer_to_parser=False 的含义：可能和 parser 双下载、双发送（它的处理器没法被温和阻止），
  这个取值只留着做真机对比实验用。
- 已知的遗憾：避让之后 parser 仍可能因它自己的防抖 / 资源 ID / 多 Bot 仲裁 / @他人 / 超限
  而不发，这时候那条链接就没人发了——只记日志，不做延迟接管。

零 parser 代码复制（parser 是 GPL-3.0）：只在运行时经 plugin_api 取它插件实例的公开方法
（search_key_pattern / is_session_allowed / parser_map 及其 [kw].parse），并且全程
except Exception——不引用它的异常类，日志里用异常类型名记账。

MIT 归属：「链接防抖 + 资源 ID 双层去重」「首段 @ 判定」「短链与 ?p= 正规解析」的思路
参考 BiliLearn（xiaoyaya191/bilibili_learning_bot, © 2025 XingYe contributors, MIT）
与本机 parser 插件的公开行为，代码全部是本插件自己写的。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

from src.app.plugin_system.api.llm_api import create_llm_request
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.plugin_api import get_plugin, is_plugin_loaded
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseEventHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision
from src.kernel.llm import ROLE, LLMPayload, Text

from ..services.bili_downloader import (
    _DOWNLOAD_SEMAPHORE,
    BiliDownloadError,
    download_bili_video,
    send_video_file,
)
from ..services.llm_utils import load_model_set
from ..services.persona import compose_persona
from ..services.material import (
    ASR_GRACE_SECONDS,
    BiliMaterialError,
    _asr_enabled_flag,  # 「ASR 到底开没开」只问这一个判据，外层时限跟着它走，不会两处判得不一样
    _redact,  # 异常文本进日志前的统一脱敏入口（和 tools/material_tool.py 用的是同一道闸门）
    build_material_markdown,
    load_credential,
)

logger = get_logger("bili_sharer_reactive")

# 本插件自己造出来的消息，message_id 都带这个前缀（services/scheduler.py 拼装，前缀由我们说了算）
SYNTHETIC_MESSAGE_PREFIX = "bili_sharer_"
# 要避让 / 要借道的那个插件叫什么：只在运行时经 plugin_api 敲门，绝不 import 它的模块
PARSER_PLUGIN_NAME = "parser"

# 一条消息最多处理几个链接：粘贴一整页链接时不至于把下载全占满
MAX_LINKS_PER_MESSAGE = 3
# 自研下载的总兜底时限：流里本来就有超时，这一层是防极端情况下挂死在那儿
DOWNLOAD_TIMEOUT = 900.0
# 接管其他平台的外层超时：parser 的 parse 是「一调用就开始下载」的黑盒，必须给它扣个上限
PARSER_PARSE_TIMEOUT = 120.0
PARSER_PATH_TIMEOUT = 240.0
# 点评这条链路的超时与输出长度上限：拿素材、问 LLM 都要有边，别把聊天上下文撑爆。
# MATERIAL_TIMEOUT 是外层兜底时限的**基础值**（不含 ASR 的估算）；[asr] 开着时
# 由 _material_timeout 自适应放宽，关着时取的就是这个基础值本身
MATERIAL_TIMEOUT = 90.0
LLM_TIMEOUT = 30.0
LLM_MATERIAL_CAP = 12000
OPINION_MAX_CHARS = 600
SUMMARY_PART_CAP = 400

# URL 的字符类里排掉空白、引号、尖括号和整片 CJK 区段（中文标点、假名、汉字、
# 兼容表意文字、全角形式）：这样「链接紧贴中文正文」时汉字不会被吸进链接里，
# _TRAILING_PUNCT 只需要负责收尾剪掉西文标点就够了。
_URL_RE = re.compile(
    r"""https?://[^\s<>"'\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]+"""
)
_BV_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
_AV_RE = re.compile(r"(?:^|[^0-9A-Za-z])av(\d{1,12})(?![0-9A-Za-z])", re.IGNORECASE)
# 链接尾巴上常挂着的标点（分享文案里总爱贴在一起）：剪掉它们才算同一条资源
_TRAILING_PUNCT = ".,;:!?、。，：；！？)）]】>」』"

# B 站自己的域名（含短链域）：live.bilibili.com 这类子域同样算 B 站资源
BILI_HOSTS = ("bilibili.com", "b23.tv", "bili2233.cn", "bilibili.cn")

_OPINION_PROMPT = """你是下面这位 Bot，请全程按这个人设说话：
{persona}
兴趣方向：{interests}

群里刚有人发了这条链接：{link}

下面是这个内容的素材（B 站字幕/弹幕/热门评论，或解析器给出的摘要）：
{material}

请用你自己的口吻，对这个内容说 1-3 句看法：
- 要有具体依据（提到素材里的实际内容），不要空洞地夸一句「有意思」；
- 不要复读标题，不要剧透式长篇，不要列清单；
- 只输出要发到群里的那几句话，不要解释你在做什么。"""


# ---------------------------------------------------------------- 数值/文本小工具


def _as_int(value: Any, default: int) -> int:
    """尽力把一个值读成整数：配置里写成 "300" 这种字符串也能认，认不出就用默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    """尽力把一个值读成小数：非数字和 NaN（自己不等于自己的那个怪值）都退回默认值。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if result != result else result


def _material_timeout(asr_cfg: Any) -> float:
    """算出「等素材最多等多久」（秒）：开关关着就短，开着就按 ASR 的真实开销放宽。

    开没开只问素材服务内部的 _asr_enabled_flag（同一个函数），这样「素材里到底跑不跑
    ASR」和「外层愿意等多久」永远不会各说各话：
    - 没有 [asr] 段 / 传进来是 None / enabled=false → 就是 MATERIAL_TIMEOUT 本身，
      关着时和从前逐字等价（超时日志里渲染出来的也是 90s）；
    - enabled=true → 基础值 + ([asr].timeout_seconds + ASR_GRACE_SECONDS)，和素材内部
      ASR 段的硬时限（material._asr_outer_timeout）是同一把尺子，默认 90 + 120 + 60 = 270s。
      （timeout_seconds 写成脏值或缺失都按 120 计；配置是 dict 形态时取不到也按 120 计）
    """
    if not _asr_enabled_flag(asr_cfg):
        return MATERIAL_TIMEOUT
    return MATERIAL_TIMEOUT + _as_float(getattr(asr_cfg, "timeout_seconds", 120), 120.0) + ASR_GRACE_SECONDS


def _text_of(value: Any, cap: int) -> str:
    """把任意值转成文本并剪到 cap 个字符以内：太长就补一个省略号，None 就是空串。"""
    if value is None:
        return ""
    text = str(value).strip()
    return text[:cap] + "…" if len(text) > cap else text


# ---------------------------------------------------------------- 链接识别


@dataclass
class Link:
    """一条被认出来的链接，装着后面每一步都要用的四件事。

    Attributes:
        raw: 消息里的原文（URL 或裸 BV/av 号），下游抓取直接拿它。
        bili: 是不是 B 站资源——True 走我们自己的下载，False 得借道 parser。
        page: 分 P 页码（从 1 数起；非 B 站固定为 1）。
        resource: 资源 ID（B 站取 BV/av 号，其他平台取规范化后的链接），防抖比的就是它。
    """

    raw: str
    bili: bool
    page: int
    resource: str

    @property
    def debounce_key(self) -> str:
        """防抖时用的名字：资源 ID 打底，分 P 大于 1 才拼上 #p 号（不同 P 算不同资源）。"""
        return f"{self.resource}#p{self.page}" if self.page > 1 else self.resource


def _host_of(url: str) -> str:
    """从链接里摘出主机名（比如 www.bilibili.com）；链接歪到解析不动时返回空串，不抛。"""
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def _host_is_bilibili(host: str) -> bool:
    """这个主机名是不是 B 站家的：bilibili.com 的子域、b23.tv / bili2233.cn 这些短链域都算。"""
    return any(host == domain or host.endswith("." + domain) for domain in BILI_HOSTS)


def is_bilibili_link(raw: str) -> bool:
    """这条东西算不算 B 站资源：B 站域名（含 b23.tv / bili2233.cn 短链）或裸 BV/av 号。

    带协议头的**先看主机名**再谈形状：外站链接的路径里也可能恰好是 av 加一串数字
    （比如 youtu.be/av123456789），主机名优先才不会把外站链接误认成 B 站、
    害它错过 parser 借道那一条路。裸号（没有协议头）照旧按 BV/av 的形状判定。
    """
    text = str(raw or "").strip()
    if not text:
        return False
    if "://" in text:
        host = (_host_of(text) or "").lower()
        if host:
            return _host_is_bilibili(host)
    if _BV_RE.search(text) or _AV_RE.search(text):
        return True
    probe = text if "://" in text else f"https://{text}"
    return _host_is_bilibili((_host_of(probe) or "").lower())


def extract_page(url: str) -> int:
    """从链接的参数里正规读出分 P 页码 p=：没有、写坏了、或者小于等于 1，都当第 1 页。

    这是踩过坑才知道的：p= 不保证是第一个参数，必须交给 urlparse + parse_qs 去解，
    用「紧跟在 bvid 后面」的正则会漏。
    """
    text = str(url or "")
    if "://" not in text:
        return 1
    try:
        query = parse_qs(urlparse(text).query)
    except ValueError:
        return 1
    return max(1, _as_int((query.get("p") or [""])[0], 1))


def _bilibili_resource(raw: str) -> str:
    """给 B 站资源起个 ID：先认 BV 号，其次 av 号，再退到 /video/<id> 这段路径，
    全都认不出就用去掉参数的链接本身。
    """
    text = str(raw or "").strip()
    matched = _BV_RE.search(text)
    if matched:
        return matched.group(0)
    matched = _AV_RE.search(text)
    if matched:
        return "av" + matched.group(1)
    probe = text if "://" in text else f"https://{text}"
    try:
        parts = [part for part in urlparse(probe).path.split("/") if part]
    except ValueError:
        parts = []
    for index, part in enumerate(parts):
        if part.lower() in ("video", "av") and index + 1 < len(parts):
            return parts[index + 1]
        if part.lower().startswith("av") and part[2:].isdigit():
            return part
    return text.split("?")[0].split("#")[0].rstrip(_TRAILING_PUNCT).rstrip("/")


def find_links(text: str) -> list[Link]:
    """在消息文本里挑候选链接（URL 和裸 BV/av 号都算），按资源去重、保持出现的先后顺序。

    已经被某个 URL 吃进去的 BV/av 号不会再数一遍，免得同一条资源被处理两次。
    Returns:
        Link 列表，B 站资源的 bili=True；整条消息没链接就是空列表。
    """
    body = str(text or "")
    if not body:
        return []
    candidates: list[str] = []
    spans: list[tuple[int, int]] = []
    for matched in _URL_RE.finditer(body):
        url = matched.group(0).rstrip(_TRAILING_PUNCT)
        if url:
            candidates.append(url)
            spans.append(matched.span())
    for pattern in (_BV_RE, _AV_RE):
        for matched in pattern.finditer(body):
            if any(start <= matched.start() < end for start, end in spans):
                continue  # 这段已经落在某个 URL 里面了，同一条资源不重复数
            # av 号的正则是「前缀字符 + 捕获号码」，要补回 av 前缀才是完整的资源名
            candidates.append("av" + matched.group(1) if pattern is _AV_RE else matched.group(0))
    links: list[Link] = []
    seen: set[str] = set()
    for item in candidates:
        bili = is_bilibili_link(item)
        page = extract_page(item) if bili else 1
        resource = (
            _bilibili_resource(item)
            if bili
            else item.split("#")[0].rstrip(_TRAILING_PUNCT).rstrip("/")
        )
        candidate = Link(raw=item, bili=bili, page=page, resource=resource)
        if not resource or candidate.debounce_key in seen:
            continue
        seen.add(candidate.debounce_key)
        links.append(candidate)
    return links


def extract_message_text(message: Any, envelope: dict) -> str:
    """把消息里的文字掏出来：先要纯文本，其次原始 content，最后从 JSON 卡片里捞链接。

    QQ 分享常常是一张 json 卡片，processed_plain_text 是空的，链接藏在卡片正文里——
    不补这最后一条路就会漏识别（覆盖面和 parser 一样，实现是自己写的）。
    """
    text = str(getattr(message, "processed_plain_text", "") or "").strip()
    if text:
        return text
    content = getattr(message, "content", "")
    if isinstance(content, str) and content.strip():
        return content.strip()
    segment = envelope.get("message_segment") if isinstance(envelope, dict) else None
    segments = [segment] if isinstance(segment, dict) else (segment if isinstance(segment, list) else [])
    for item in segments:
        if not isinstance(item, dict) or str(item.get("type", "")).lower() != "json":
            continue
        data = item.get("data")
        if isinstance(data, dict):
            data = data.get("data") or data.get("url") or ""
        matched = _URL_RE.search(str(data or ""))
        if matched:
            return matched.group(0).rstrip(_TRAILING_PUNCT)
    return ""


# ---------------------------------------------------------------- 会话过滤 / 防自触发


def pass_session_filter(
    stream_id: str, whitelist: Iterable[str] | None, blacklist: Iterable[str] | None
) -> bool:
    """这个会话我可以理吗：白名单非空时只认白名单（它比黑名单大）；白名单空着才看黑名单。"""
    allow = [str(item) for item in (whitelist or []) if str(item)]
    if allow:
        return bool(stream_id) and str(stream_id) in allow
    deny = [str(item) for item in (blacklist or []) if str(item)]
    if deny and str(stream_id) in deny:
        return False
    return True


def is_synthetic_message(message_id: Any) -> bool:
    """防自触发第一道：这条是不是我自己 scheduler 造出来的合成消息（看 message_id 前缀）。"""
    return str(message_id or "").startswith(SYNTHETIC_MESSAGE_PREFIX)


def resolve_self_id(envelope: dict, message: Any) -> str:
    """找出「我自己是几号」：依次翻 envelope 的 additional_config、message_info、顶层，
    最后退到 raw_data；哪儿有值用哪儿，全都没有就是空串。
    """
    env = envelope if isinstance(envelope, dict) else {}
    info = env.get("message_info")
    info = info if isinstance(info, dict) else {}
    additional = info.get("additional_config")
    additional = additional if isinstance(additional, dict) else {}
    raw = getattr(message, "raw_data", None)
    raw = raw if isinstance(raw, dict) else {}
    for holder, key in ((additional, "self_id"), (info, "self_id"), (env, "self_id"), (raw, "self_id")):
        value = holder.get(key)
        if value:
            return str(value)
    return ""


def is_from_self(sender_id: Any, self_id: Any) -> bool:
    """防自触发第二道：发送者就是我本人吗？self_id 未知时一律说不是——不能凭空认亲。"""
    sender = str(sender_id or "")
    myself = str(self_id or "")
    return bool(sender) and bool(myself) and sender == myself


def first_at_target_id(envelope: dict) -> str:
    """消息开头那个 @ 点的是谁（第一段不是 at 就返回空串）。

    mofox-wire 的 at 段 data 长这样「昵称:用户 ID」；顺手也认 dict 形态（qq / user_id）。
    """
    env = envelope if isinstance(envelope, dict) else {}
    segment = env.get("message_segment")
    if isinstance(segment, list) and segment:
        first = segment[0]
    elif isinstance(segment, dict):
        first = segment
    else:
        return ""
    if not isinstance(first, dict) or first.get("type") != "at":
        return ""
    data = first.get("data")
    if isinstance(data, dict):
        return str(data.get("qq") or data.get("user_id") or "")
    text = str(data or "").strip()
    if not text:
        return ""
    return text.rsplit(":", 1)[-1].strip() if ":" in text else text


def is_at_other_bot(envelope: dict, self_id: Any) -> bool:
    """防自触发第三道：开头 @ 的是别的 bot —— 那句是讲给别人的悄悄话，我不该接。"""
    myself = str(self_id or "")
    target = first_at_target_id(envelope)
    return bool(target) and bool(myself) and target != myself


def self_generated_reason(message: Any, envelope: dict) -> str:
    """防自触发的总闸：三道保险任意命中就返回一句原因（写日志用），全没命中返回空串。"""
    if is_synthetic_message(getattr(message, "message_id", "")):
        return "合成消息（message_id 前缀 bili_sharer_）"
    self_id = resolve_self_id(envelope, message)
    if is_from_self(getattr(message, "sender_id", ""), self_id):
        return "自身消息（sender==self_id）"
    if is_at_other_bot(envelope, self_id):
        return f"首段 @ 其他 bot（{first_at_target_id(envelope)}）"
    return ""


# ---------------------------------------------------------------- 防抖 / 频率限制


class DebounceStore:
    """一段时间内「同一个会话 + 同一条资源」只放过一次的小本本。

    记在内存 dict 里，每条都带一个到期时刻；隔一阵子清一次过期条目，
    条目数顶到上限时会立刻多清一轮，不让它无界长大。
    """

    def __init__(
        self,
        window_seconds: float = 300.0,
        clock: Any = time.monotonic,
        prune_interval: float = 60.0,
        max_entries: int = 4096,
    ) -> None:
        self.window_seconds = max(0.0, _as_float(window_seconds, 0.0))
        self.clock = clock
        self.prune_interval = max(1.0, _as_float(prune_interval, 60.0))
        self.max_entries = max(64, _as_int(max_entries, 4096))
        self._expires: dict[str, float] = {}
        self._next_prune = 0.0

    def hit(self, stream_id: str, key: str) -> bool:
        """窗口内已经见过这条资源就说 True（该跳过）；没见过就登记一笔、说 False。

        窗口长度是 0（等于不防抖）或者资源 key 是空的，都直接 False——照旧放行。
        """
        now = float(self.clock())
        self._prune_if_due(now)
        if self.window_seconds <= 0 or not key:
            return False
        full = f"{stream_id}|{key}"
        expires = self._expires.get(full)
        if expires is not None and expires > now:
            return True
        self._expires[full] = now + self.window_seconds
        if len(self._expires) > self.max_entries:  # 本子太厚了，立刻清一次腾地方，绝不无界长大
            self.prune(now)
        return False

    def prune(self, now: float | None = None) -> int:
        """把所有过期的条目擦掉，顺手把下次清理的时刻往后推，返回擦掉了多少条。"""
        moment = float(self.clock()) if now is None else float(now)
        expired = [key for key, expires in self._expires.items() if expires <= moment]
        for key in expired:
            self._expires.pop(key, None)
        self._next_prune = moment + self.prune_interval
        return len(expired)

    def size(self) -> int:
        """现在本子上记了几条（测试和看状态用）。"""
        return len(self._expires)

    def _prune_if_due(self, now: float) -> None:
        if now >= self._next_prune:
            self.prune(now)


class OpinionRateLimiter:
    """一个会话在一段时间里最多让我点评 N 次——话太多是会刷屏的呀。

    每个会话记一串时间戳，滑出窗口的那些会被丢掉；窗口和条数都能按配置刷新。
    """

    def __init__(
        self,
        limit: int = 3,
        window_seconds: float = 600.0,
        clock: Any = time.monotonic,
        prune_interval: float = 60.0,
    ) -> None:
        self.clock = clock
        self.prune_interval = max(1.0, _as_float(prune_interval, 60.0))
        self._hits: dict[str, list[float]] = {}
        self._next_prune = 0.0
        self.configure(limit, window_seconds)

    def configure(self, limit: int, window_seconds: float) -> None:
        """按配置刷新「几条 / 多久」；写歪了就归到下限，和配置校验用的是同一种防御脾气。"""
        self.limit = max(1, _as_int(limit, 1))
        self.window_seconds = max(60.0, _as_float(window_seconds, 600.0))

    def try_acquire(self, stream_id: str) -> bool:
        """还有额度就占一个槽、返回 True；已经超了返回 False（这一句也不占额度）。"""
        now = float(self.clock())
        self._prune_if_due(now)
        hits = [moment for moment in self._hits.get(stream_id, []) if now - moment < self.window_seconds]
        if len(hits) >= self.limit:
            self._hits[stream_id] = hits
            return False
        hits.append(now)
        self._hits[stream_id] = hits
        return True

    def release(self, stream_id: str) -> None:
        """把最近占的那个槽还回去：素材拿不到、LLM 没回话，不该白白吃掉一次额度。"""
        hits = self._hits.get(stream_id)
        if hits:
            hits.pop()

    def prune(self, now: float | None = None) -> int:
        """清掉那些记录全都滑出窗口的会话，返回擦掉了几位会话（顺便排下一次清理的时刻）。"""
        moment = float(self.clock()) if now is None else float(now)
        removed = 0
        for stream_id in list(self._hits):
            hits = [item for item in self._hits[stream_id] if moment - item < self.window_seconds]
            if hits:
                self._hits[stream_id] = hits
            else:
                self._hits.pop(stream_id, None)
                removed += 1
        self._next_prune = moment + self.prune_interval
        return removed

    def size(self) -> int:
        """现在有几位会话被我记着账（测试和看状态用）。"""
        return len(self._hits)

    def _prune_if_due(self, now: float) -> None:
        if now >= self._next_prune:
            self.prune(now)


# ---------------------------------------------------------------- parser 借道（运行时，零引用其模块）


def get_parser_plugin() -> Any | None:
    """去要 parser 的插件实例（避让五合一的前两条检查）：没加载、拿不到、
    或者少了我要用的那两个公开方法，都当它缺席，返回 None。
    """
    try:
        if not is_plugin_loaded(PARSER_PLUGIN_NAME):
            return None
        plugin = get_plugin(PARSER_PLUGIN_NAME)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[被动] 查询 parser 插件失败（按缺席处理）：{type(exc).__name__}: {exc}")
        return None
    if plugin is None:
        return None
    if not all(callable(getattr(plugin, name, None)) for name in ("search_key_pattern", "is_session_allowed")):
        logger.debug("[被动] parser 实例缺 search_key_pattern / is_session_allowed，按缺席处理")
        return None
    return plugin


def match_parser_link(parser_plugin: Any, text: str) -> Any:
    """五合一第三条：parser 的关键词表里有没有命中这条消息？命中返回 (关键词, 匹配)，否则 None。
    它抛异常也按「未命中」处理。
    """
    if parser_plugin is None:
        return None
    try:
        return parser_plugin.search_key_pattern(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[被动] parser 关键词匹配异常（按未命中处理）：{type(exc).__name__}: {exc}")
        return None


def parser_claims_keyword(parser_plugin: Any, keyword: Any) -> bool:
    """五合一第四条：这个关键词背后的平台，parser 真的认领吗（防御性检查）。

    只看它的公开面 parser_map：命中的关键词在里面没有条目、或者条目身上没有
    可调用的 parse，就按「没认领」处理（那就不避让，本插件接手）。这里绝不抛。

    为什么留着这条：现版 parser 的 parser_map 和关键词表是同一段启用逻辑生成的，
    命中就一定认领，所以眼下它不会翻转任何判定。留着是给将来兜底的——万一 parser
    允许「模式命中但平台禁用」，本插件会自动接管，不至于两边都不发。
    至于残留的「两边都不发」（parser 自己的仲裁 / 防抖 / parse 失败），见模块开头
    写的已知限制，不是这一条能消除的。
    """
    if parser_plugin is None or keyword is None:
        return False
    try:
        parser_map = getattr(parser_plugin, "parser_map", None)
        if not isinstance(parser_map, dict):
            logger.debug("[被动] parser 实例缺 parser_map（按未认领处理，不避让）")
            return False
        handler = parser_map.get(keyword)
        return handler is not None and callable(getattr(handler, "parse", None))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[被动] parser 认领检查异常（按未认领处理）：{type(exc).__name__}: {exc}")
        return False


def parser_allows_session(parser_plugin: Any, stream_id: str) -> bool:
    """五合一第五条：parser 自己愿意处理这个会话吗（问的是它的白/黑名单）。
    它抛异常就按「不愿意」处理。
    """
    if parser_plugin is None:
        return False
    try:
        return bool(parser_plugin.is_session_allowed(stream_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[被动] parser 会话判定异常（按不允许处理）：{type(exc).__name__}: {exc}")
        return False


def should_defer_to_parser(
    parser_plugin: Any, stream_id: str, text: str, *, defer_enabled: bool
) -> tuple[bool, str]:
    """要不要给 parser 让路：开关开着 + 它在场 + 关键词命中 + 它认领该平台 + 它愿意管这个会话，
    五条同时成立才让。其中「认领」是防御性的一条（现版 parser 命中必认领），
    不承诺已经消除「两边都不发」那种情况。返回值是 (让不让, 一句原因)。
    """
    if not defer_enabled:
        return False, "defer_to_parser=False（本插件也发一份，可能与 parser 双下载、双发送）"
    if parser_plugin is None:
        return False, "parser 缺席（未加载或不可用）"
    hit = match_parser_link(parser_plugin, text)
    if not hit:
        return False, "消息未命中 parser 关键词"
    keyword = hit[0] if isinstance(hit, (tuple, list)) and len(hit) >= 1 else None
    if not parser_claims_keyword(parser_plugin, keyword):
        return False, f"parser 未认领关键词 {keyword!r}（parser_map 无该条目），本插件接管"
    if not parser_allows_session(parser_plugin, stream_id):
        return False, "parser 不处理该会话（其白/黑名单）"
    return True, "避让 parser（五合一成立）"


def parser_summary(res: Any) -> str:
    """把 parser 已经做好的产物读一读，拼成写简评用的素材（只读属性，不再 parse、不下载封面）。"""
    if res is None:
        return ""
    parts: list[str] = []
    header = _text_of(getattr(res, "header", None), SUMMARY_PART_CAP)
    if header:
        parts.append(header)
    title = _text_of(getattr(res, "title", None), SUMMARY_PART_CAP)
    if title and title not in header:
        parts.append(f"标题：{title}")
    body = _text_of(getattr(res, "text", None), SUMMARY_PART_CAP)
    if body:
        parts.append(f"简介：{body}")
    extra = getattr(res, "extra", None)
    if isinstance(extra, dict):
        info = _text_of(extra.get("info"), SUMMARY_PART_CAP)
        if info:
            parts.append(f"补充信息：{info}")
    return "\n".join(parts)[:LLM_MATERIAL_CAP]


# 加载成功的模型集按 model_name 长期留着复用；失败只记一个时间戳，冷却窗口一过，
# 下一条点评会重新试着加载——不然第一条刚好碰上模型不可用，自动点评就被永久判了死刑。
_MODEL_SET_CACHE: dict[str, Any] = {}
_MODEL_SET_FAILED_AT: dict[str, float] = {}
_MODEL_SET_FAIL_TTL = 60.0


def load_opinion_model_set(builder_cfg: Any) -> Any:
    """取点评要用的模型集：成功才缓存起来，失败就进冷却窗口，窗口过后重试。

    输入：builder 配置段（要的是 model_name）。没配名字或拿不到模型就返回 None，
    调用方静默跳过这一次点评。冷却窗口只是为了别让模型长时间不可用时每条点评
    都重跑一遍加载（日志会刷满屏），并不是把「失败」这件事缓存下来。
    """
    name = str(getattr(builder_cfg, "model_name", "") or "")
    if not name:
        return None
    if name in _MODEL_SET_CACHE:
        return _MODEL_SET_CACHE[name]
    now = time.monotonic()
    failed_at = _MODEL_SET_FAILED_AT.get(name)
    if failed_at is not None and now - failed_at < _MODEL_SET_FAIL_TTL:
        logger.debug(f"[被动] 点评模型 {name} 在失败冷却窗口内，本轮跳过")
        return None
    model_set = load_model_set(builder_cfg, label="被动点评", consequence="跳过自动点评")
    if model_set is None:
        _MODEL_SET_FAILED_AT[name] = now
    else:
        _MODEL_SET_CACHE[name] = model_set
        _MODEL_SET_FAILED_AT.pop(name, None)
    return model_set


async def generate_opinion(
    *, material: str, persona: str, interests: Any, link: str, builder_cfg: Any
) -> str:
    """把人设 + 兴趣方向 + 素材交给 LLM，换回一段 1-3 句的看法。

    模型不可用、超时、或者它脾气不好抛异常，一律返回空串：由调用方安静跳过，
    不往群里扔半句碎话，也不把异常往外递。
    """
    model_set = load_opinion_model_set(builder_cfg)
    if model_set is None:
        logger.debug("[被动] 点评模型不可用，跳过")
        return ""
    body = str(material or "").strip()
    if not body:
        return ""
    prompt = _OPINION_PROMPT.format(
        persona=str(persona or "").strip() or "一个普通的群友",
        interests="、".join(str(item) for item in (interests or [])) or "不限",
        link=str(link or "")[:200],
        material=body[:LLM_MATERIAL_CAP],
    )
    try:
        request = create_llm_request(model_set, request_name="bilibili_sharer:reactive_opinion")
        request.add_payload(LLMPayload(ROLE.USER, Text(prompt)))
        response = await asyncio.wait_for(request.send(stream=False), timeout=LLM_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[被动] 点评生成失败：{type(exc).__name__}: {exc}")
        return ""
    return _text_of(getattr(response, "message", ""), OPINION_MAX_CHARS)


# ---------------------------------------------------------------- 事件处理器


class ReactiveHandler(BaseEventHandler):
    """被动链路处理器本尊：守着群消息，按开关替群友把视频发出来，或者顺口点评一句。
    """

    # 统一用新式的类属性 name / description：旧写法 handler_name / handler_description
    # 会被框架桥接，而且类一定义就发 DeprecationWarning（《AI 插件编写规范》§11 第 5 条
    # 明确不许留旧名），写法照本机其它 event_handler 组件的现例来。
    # 名字本身一个字没动：还是 manifest.include 与 check_manifest.py 里的 "bili_sharer_reactive"。
    name: str = "bili_sharer_reactive"
    description: str = "被动链路：识别链接后按开关发视频/自动点评"

    weight: int = 40
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [EventType.ON_MESSAGE_RECEIVED]

    def __init__(self, plugin: Any) -> None:
        super().__init__(plugin)
        self._debounce = DebounceStore(window_seconds=300.0)
        self._limiter = OpinionRateLimiter(limit=3, window_seconds=600.0)

    # ---------------------------------------------------------------- 事件入口

    async def execute(  # type: ignore[override]
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        # 下载、合流、问 LLM 动辄几十秒，早超过事件订阅的超时预算；
        # 所以整条链路丢给后台 task，这里立刻回 SUCCESS 让事件流继续走（绝不拦下这条消息）。
        asyncio.create_task(self._safe_process(params))
        return EventDecision.SUCCESS, params

    async def _safe_process(self, params: dict[str, Any]) -> None:
        """后台 task 的保险外壳：里面翻出什么浪都只写日志，绝不冒泡出去打断事件流。"""
        try:
            await self._process(params)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[被动] 处理出错：{type(exc).__name__}: {exc}", exc_info=True)

    # ---------------------------------------------------------------- 主流程

    async def _process(self, params: dict[str, Any]) -> None:
        cfg = self._config()
        if cfg is None:
            return
        if not bool(getattr(getattr(cfg, "plugin", None), "enabled", True)):
            return
        want_video = bool(getattr(cfg.video_send, "reactive_enabled", False))
        want_opinion = bool(getattr(cfg.opinion, "reactive_auto", False))
        if not want_video and not want_opinion:
            return  # 两个开关都没开——默认基线，我在这里一个动作都不做

        message = params.get("message") if isinstance(params, dict) else None
        envelope = params.get("envelope") if isinstance(params, dict) else None
        envelope = envelope if isinstance(envelope, dict) else {}
        stream_id = str(getattr(message, "stream_id", "") or "")
        if not message or not stream_id:
            return

        filt = cfg.reactive_filter
        if not pass_session_filter(stream_id, filt.whitelist, filt.blacklist):
            logger.debug(f"[被动] 会话不在白名单 / 在黑名单，跳过：{stream_id}")
            return

        reason = self_generated_reason(message, envelope)
        if reason:
            logger.debug(f"[被动] 防自触发跳过（{reason}）")
            return

        text = extract_message_text(message, envelope)
        links = find_links(text) if text else []
        if not links:
            return

        self._sync_windows(filt)
        # platform 只在这里取一次，两条动作分支原样传下去，免得两处各拿各的。
        # 空串在 send_api 眼里是 falsy，和 None 同义（会按 stream_id 去推断平台），
        # 所以点评分支不必再多写一次「空就当没给」。
        platform = str(getattr(message, "platform", "") or "")

        parser_plugin = get_parser_plugin() if want_video else None
        defer, defer_why = (False, "")
        if want_video:
            defer, defer_why = should_defer_to_parser(
                parser_plugin,
                stream_id,
                text,
                defer_enabled=bool(getattr(cfg.video_send, "defer_to_parser", True)),
            )

        reused: tuple[Link, Any] | None = None
        if want_video:
            if defer:
                logger.info(f"[被动] 发视频{defer_why}：{text[:80]}")
            else:
                reused = await self._send_videos(
                    cfg=cfg,
                    links=links,
                    text=text,
                    stream_id=stream_id,
                    platform=platform,
                    parser_plugin=parser_plugin,
                )

        if want_opinion:
            await self._send_opinion(cfg, links, stream_id, platform, reused=reused)

    # ---------------------------------------------------------------- 发视频

    async def _send_videos(
        self,
        *,
        cfg: Any,
        links: list[Link],
        text: str,
        stream_id: str,
        platform: str,
        parser_plugin: Any,
    ) -> tuple[Link, Any] | None:
        """一条条链接轮流处理：防抖窗口内的先跳过，B 站走自研下载，其他平台借道 parser。

        输入：配置、链接列表、消息原文、会话 ID、平台名、parser 实例。
        输出：最多一条「已经 parse 过的其他平台产物」，让点评分支省一次解析；没有就返回 None。
        platform 用 _process 里取好的值，原样往下传。
        """
        reused: tuple[Link, Any] | None = None
        for link in links[:MAX_LINKS_PER_MESSAGE]:
            if self._debounce.hit(stream_id, link.debounce_key):
                logger.info(f"[被动] 资源 {link.debounce_key} 在防抖窗口内，跳过")
                continue
            if link.bili:
                await self._send_bilibili_video(cfg, link, stream_id, platform)
                continue
            if parser_plugin is None:
                logger.debug(f"[被动] parser 缺席，其他平台链接跳过：{link.raw[:80]}")
                continue
            res = await self._send_parser_video(
                parser_plugin, match_parser_link(parser_plugin, text), link, stream_id, platform
            )
            if res is not None and reused is None:
                reused = (link, res)
        return reused

    async def _send_bilibili_video(self, cfg: Any, link: Link, stream_id: str, platform: str) -> None:
        """B 站链接走这条路：自己下载，再把文件发出去。

        产物是我们自己的，delete_after=True 发完就删；几个人同时下载由服务侧
        那个全局 Semaphore(2) 统一管着。失败与超时都只记日志、安静返回。
        """
        try:
            path, meta = await asyncio.wait_for(
                download_bili_video(link.raw, page_num=link.page, cfg=cfg), timeout=DOWNLOAD_TIMEOUT
            )
        except BiliDownloadError as exc:
            logger.warning(f"[被动] B 站视频下载失败：{exc}")
            return
        except asyncio.TimeoutError:
            logger.warning(f"[被动] B 站视频下载超时（{DOWNLOAD_TIMEOUT:.0f}s）：{link.raw[:80]}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[被动] B 站视频下载异常：{type(exc).__name__}: {exc}", exc_info=True)
            return
        info = meta if isinstance(meta, dict) else {}
        logger.info(
            f"[被动] B 站下载完成 {link.resource} P{link.page}："
            f"{_text_of(info.get('quality'), 20)} / {_as_int(info.get('size'), 0)} 字节"
        )
        sent = await send_video_file(Path(path), stream_id, platform, delete_after=True)
        if not sent:
            # 发送服务内部已经记过日志，这里从调用侧补一句：到底发没发出去要说得清。
            logger.warning(
                f"[被动] B 站视频发送失败（send_video_file=False，详见服务侧日志）："
                f"{link.resource} P{link.page} -> {stream_id}"
            )

    async def _send_parser_video(
        self, parser_plugin: Any, hit: Any, link: Link, stream_id: str, platform: str
    ) -> Any | None:
        """其他平台走这条路：借 parser 的下载成果把文件发出去，自己不重下一遍。

        全程 except Exception——不引用它的异常类（GPL 边界，也不能复制它的代码）；
        外层扣两个超时，再排全局 Semaphore(2) 那扇小门；video_contents 是空的就跳过。
        产物是 parser 的缓存文件，所以只发不删（delete_after=False）。
        返回它的 parse 结果给点评分支复用，什么都没 parse 成就返回 None。
        """
        if not isinstance(hit, (tuple, list)) or len(hit) != 2:
            logger.debug(f"[被动] 未命中 parser 关键词，跳过：{link.raw[:80]}")
            return None
        keyword, searched = hit
        parser_map = getattr(parser_plugin, "parser_map", None)
        parser = parser_map.get(keyword) if isinstance(parser_map, dict) else None
        parse = getattr(parser, "parse", None)
        if not callable(parse):
            logger.info(f"[被动] parser_map 无关键词 {keyword!r}（该平台未启用），跳过")
            return None
        path: Any = None
        async with _DOWNLOAD_SEMAPHORE:
            try:
                res = await asyncio.wait_for(parse(keyword, searched), timeout=PARSER_PARSE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(f"[被动] parser 解析超时（{PARSER_PARSE_TIMEOUT:.0f}s）：{link.raw[:80]}")
                return None
            except Exception as exc:  # noqa: BLE001 —— 解析失败是日常噪音，记一行就好，不甩 traceback
                logger.warning(f"[被动] parser 解析跳过：{type(exc).__name__}: {exc}")
                return None
            if res is None:
                return None
            # 把 parser 报出的资源 ID 也记进防抖小本本：同一条资源就不发第二遍了。
            try:
                resource_id = str(res.get_resource_id() or "")
            except Exception as exc:  # noqa: BLE001
                resource_id = ""
                logger.debug(f"[被动] 取资源 ID 失败（跳过该层防抖）：{type(exc).__name__}: {exc}")
            if resource_id and self._debounce.hit(stream_id, f"parser:{resource_id}"):
                logger.info(f"[被动] 资源 {resource_id} 在防抖窗口内，跳过发送")
                return res
            try:
                contents = list(res.video_contents or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[被动] 读取 parser 视频内容失败：{type(exc).__name__}: {exc}")
                return res
            if not contents:
                logger.info(f"[被动] {link.raw[:60]} 无视频内容（图文/纯文本），跳过发送")
                return res
            get_path = getattr(contents[0], "get_path", None)
            if not callable(get_path):
                logger.warning("[被动] parser 视频内容没有 get_path，跳过")
                return res
            try:
                path = await asyncio.wait_for(get_path(), timeout=PARSER_PATH_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(f"[被动] 等 parser 下载超时（{PARSER_PATH_TIMEOUT:.0f}s）：{link.raw[:80]}")
                return res
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[被动] parser 下载失败：{type(exc).__name__}: {exc}")
                return res
        if not path:
            return res
        sent = await send_video_file(Path(path), stream_id, platform, delete_after=False)
        if not sent:
            # 调用侧补一句可见性（parser 的缓存文件照旧由它自己处置，我们只发不删）。
            logger.warning(
                f"[被动] parser 缓存产物发送失败（send_video_file=False，详见服务侧日志）："
                f"{link.raw[:80]} -> {stream_id}"
            )
        return res

    # ---------------------------------------------------------------- 自动点评

    async def _send_opinion(
        self,
        cfg: Any,
        links: list[Link],
        stream_id: str,
        platform: str,
        *,
        reused: tuple[Link, Any] | None,
    ) -> None:
        """自动点评这一段：挑一条链接，取素材，问 LLM 要一句看法，发回来源会话。

        点评不给 parser 让路——它家不点评，所以不会撞车重复发；一条消息最多发一句。
        B 站的素材由 _fetch_material 去取（那边把 [asr] 段交给素材服务，外层时限随之放宽）；
        其他平台只复用刚刚 parse 过的产物。频率超限、素材为空、看法为空都会安静收场。"""
        bili_link = next((item for item in links if item.bili), None)
        if bili_link is None and reused is None:
            logger.debug("[被动] 其他平台没有「不触发下载」的元信息通路，默认不点评")
            return
        if not self._limiter.try_acquire(stream_id):
            logger.info(
                f"[被动] 会话 {stream_id} 点评已达频率上限（{self._limiter.limit} 次/"
                f"{self._limiter.window_seconds / 60:.0f} 分钟），跳过"
            )
            return
        if bili_link is not None:
            material = await self._fetch_material(cfg, bili_link)
            link_text = bili_link.raw
        else:
            material = parser_summary(reused[1])
            link_text = reused[0].raw
        if not material:
            self._limiter.release(stream_id)
            return
        opinion = await generate_opinion(
            material=material,
            # 人设默认就是 bot 的全局人格（compose_persona 自带降级，不会抛出来）
            persona=compose_persona(getattr(cfg, "persona", None)),
            interests=getattr(cfg.persona, "interests", None),
            link=link_text,
            builder_cfg=cfg.builder,
        )
        if not opinion:
            self._limiter.release(stream_id)
            return
        try:
            await send_text(opinion, stream_id=stream_id, platform=platform)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[被动] 点评发送失败：{type(exc).__name__}: {exc}")
            return
        logger.info(f"[被动] 已自动点评 {link_text[:60]}：{opinion[:60]}")

    async def _fetch_material(self, cfg: Any, link: Link) -> str:
        """替点评取一份 B 站素材：条数与长度限幅、服务侧带 TTL 缓存、[asr] 段原样递过去。

        [asr] 段用 getattr 防御着取：Bot 重启生效之前，内存里的旧配置可能还没这个属性，
        取不到就是 None（素材就不做语音转写）。递过去之后由素材服务按 [asr].enabled
        自己决定跑不跑，这里的兜底时限也跟着同一个判据放宽。失败一律返回空串，不抛。
        """
        material_cfg = cfg.material
        asr_cfg = getattr(cfg, "asr", None)
        material_timeout = _material_timeout(asr_cfg)
        try:
            credential = load_credential(cfg.bili_credential)
        except Exception as exc:  # noqa: BLE001
            # 异常文本一律先脱敏：拿不到 Credential 对象就拿配置段的字段值去抹，
            # 素材侧那份登记册还兜着从文件读来的值——和 tools/material_tool.py 用同一道闸门。
            logger.warning(
                f"[被动] 凭据加载失败，按匿名访问："
                f"{_redact(f'{type(exc).__name__}: {exc}', cfg.bili_credential)}"
            )
            credential = None
        try:
            return await asyncio.wait_for(
                build_material_markdown(
                    link.raw,
                    include_danmaku=True,
                    include_comments=True,
                    include_timestamps=bool(getattr(material_cfg, "include_timestamps", False)),
                    danmaku_limit=_as_int(getattr(material_cfg, "danmaku_limit", 200), 200),
                    comment_limit=_as_int(getattr(material_cfg, "comment_limit", 15), 15),
                    max_chars=_as_int(getattr(material_cfg, "max_output_chars", 10000), 10000),
                    credential=credential,
                    asr_cfg=asr_cfg,
                ),
                timeout=material_timeout,
            )
        except BiliMaterialError as exc:
            # 照样先抹凭据再写日志——README 里承诺过的事，实现这边得对上。
            logger.warning(f"[被动] 素材获取失败：{_redact(_redact(exc, credential), cfg.bili_credential)}")
            return ""
        except asyncio.TimeoutError:
            logger.warning(f"[被动] 素材获取超时（{material_timeout:.0f}s）：{link.raw[:80]}")
            return ""
        except Exception as exc:  # noqa: BLE001
            # 这一条也一样：先过 material._redact 抹干净，再决定写日志。
            logger.warning(
                f"[被动] 素材获取异常："
                f"{_redact(_redact(f'{type(exc).__name__}: {exc}', credential), cfg.bili_credential)}"
            )
            return ""

    # ---------------------------------------------------------------- 辅助

    def _config(self) -> Any | None:
        """把插件配置对象拿过来；还没就绪就返回 None 并记一条 DEBUG，这里绝不抛。"""
        cfg = getattr(self.plugin, "config", None)
        if cfg is None:
            logger.debug("[被动] 插件配置未就绪，跳过")
        return cfg

    def _sync_windows(self, filt: Any) -> None:
        """把 [reactive_filter] 里的窗口参数抄进防抖小本本和点评限流器（写歪的先归到下限）。
        每处理一条消息就同步一次，所以改完配置不必重启也生效。
        """
        self._debounce.window_seconds = max(0.0, _as_float(getattr(filt, "debounce_seconds", 300), 300.0))
        window_minutes = max(1.0, _as_float(getattr(filt, "opinion_window_minutes", 10), 10.0))
        self._limiter.configure(_as_int(getattr(filt, "opinion_rate_limit", 3), 3), window_minutes * 60.0)
