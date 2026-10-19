"""消息构建 — 教 LLM 写一句推荐语，再把视频打扮成最终要发出的消息。

Bot 推视频前怎么开口？看 [opinion] 段：recommend_enabled=False 直接
沉默（只发标题＋链接）；否则按 push_mode 分三路——off 不点评 /
simple 随口一句轻推荐 / deep 看完素材再点评内容。写错的值悄悄回退 simple 并记一条 warning。

deep 的素材与时限都归素材服务管（限幅、credential、TTL 缓存）：素材或模型
不可用就退回 simple；LLM 不回话就直接用模板文案，不叫它第二次。[asr] 开启时
deep 的外层时限随之自动放宽（见 _deep_material_timeout）。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from src.app.plugin_system.api.llm_api import create_llm_request
from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMPayload, ROLE, Text

from .services.llm_utils import load_model_set
from .services.material import (
    ASR_GRACE_SECONDS,
    BiliMaterialError,
    _asr_enabled_flag,  # 「要不要跑 ASR」与素材服务内部同源判断，外层等待时长才跟得上
    _redact,  # 异常文本进日志前抹掉敏感信息的入口（聊天工具也用它）
    build_material_markdown,
    load_credential,
)

logger = get_logger("bili_sharer_builder")

_COMMENT_PROMPT = """你是下面这位 Bot，请全程按这个人设说话：
{persona}

你要向朋友推荐一个 Bilibili 视频。

视频标题：{title}
视频描述：{description}

请用你的口吻写一句推荐语，要符合你的性格特点。
注意：只要一句话，生动有趣，不要太长。"""

_DEEP_COMMENT_PROMPT = """你是下面这位 Bot，请全程按这个人设说话：
{persona}

你要向朋友推荐一个 Bilibili 视频。

视频标题：{title}
视频描述：{description}

下面是这个视频的素材（字幕/弹幕/热门评论，可能被截断）：
{material}

请基于素材用你的口吻写一两句推荐点评，要符合你的性格特点：
- 要有具体依据（提到素材里实际说过的内容），不要复读标题、不要空洞地夸；
- 生动有趣，不要剧透式长篇，不要列清单；
- 只输出要发出去的那句话，不要解释你在做什么。"""

# push_mode 只认这三个答案（[opinion].push_mode），写成别的都按 simple 待
_PUSH_MODES = ("off", "simple", "deep")
# deep 抓素材的整体兜底超时基础值（秒）：再卡最多 90 秒就放弃，服务内部各环节另有自己的超时；
# [asr] 开启时由 _deep_material_timeout 自动放宽
_DEEP_MATERIAL_TIMEOUT_SECS = 90.0
# 等 deep 点评的 LLM 回话最多 30 秒，和 simple 一个节奏
_DEEP_LLM_TIMEOUT_SECS = 30.0
# 素材拼进 prompt 的字符上限（素材服务侧已按 max_output_chars 裁过一轮）
_DEEP_MATERIAL_CAP = 12000
# deep 点评输出的字数上限，免得长文把下游聊天记录撑坏
_DEEP_COMMENT_MAX_CHARS = 600

_PREFIX_EMOJIS = ["📺", "🎬", "▶️", "🎮", "🎵", "📹"]


def _as_int(value: Any, default: int) -> int:
    """把值宽松地变成 int——配置里 120 常被写成字符串 "120" 呢。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _deep_material_timeout(asr_cfg: Any) -> float:
    """deep 素材抓取的外层兜底时限（秒）——跟着 [asr] 开关自动放宽。

    开关判据直接复用素材服务内部的 _asr_enabled_flag（同一个函数）：这样
    「素材里会不会真的跑 ASR」和「外层愿意等多久」永远不会分成两套说法。
    - 无 [asr] 段 / asr_cfg=None / enabled=false → 恰为 _DEEP_MATERIAL_TIMEOUT_SECS；
    - enabled=true → 基础值 + ([asr].timeout_seconds + ASR_GRACE_SECONDS)，与素材内部
      ASR 硬时限（material._asr_outer_timeout）同口径，默认 90+120+60 = 270s。
      （timeout_seconds 缺失或写成脏值时按 120 计）
    """
    if not _asr_enabled_flag(asr_cfg):
        return _DEEP_MATERIAL_TIMEOUT_SECS
    return (
        _DEEP_MATERIAL_TIMEOUT_SECS
        + _as_int(getattr(asr_cfg, "timeout_seconds", 120), 120)
        + ASR_GRACE_SECONDS
    )


class MessageBuilder:
    """分享消息的造物工坊：一边写推荐语，一边把消息打扮成最终发出的样子。"""

    def __init__(self, cfg: Any = None) -> None:
        # 模型加载失败不拖垮整个插件——build_comment 会自己改用模板文案。
        # 加载逻辑与过滤器共用 load_model_set，extra_params 已在其中合并好。
        self._model_set: Any = load_model_set(
            cfg, label="评论", consequence="将使用模板文案",
        )

    @staticmethod
    def _normalize_push_mode(raw: Any) -> str:
        """把 push_mode 收拾成干净值：None/空白静默按 simple；认不得的值也回退 simple，
        但会记一条 warning 提醒配置写错了。
        """
        if raw is None:
            return "simple"
        text = str(raw).strip().lower()
        if not text:
            return "simple"
        if text not in _PUSH_MODES:
            logger.warning(
                f"[opinion].push_mode 非法值 {raw!r}，已归一化回退 simple（合法值：off/simple/deep）"
            )
            return "simple"
        return text

    async def build_comment(
        self,
        video: dict[str, Any],
        persona: str,
        *,
        opinion_cfg: Any = None,
        material_cfg: Any = None,
        credential_cfg: Any = None,
        asr_cfg: Any = None,
    ) -> str:
        """按 [opinion] 段的决策顺序生成推送推荐语。

        Args:
            video: 视频信息字典（title/description/url/bvid/author…）。
            persona: 人设文本——告诉模型「你现在是谁」。
            opinion_cfg: [opinion] 段；None 时保持旧行为（等同 simple）。
            material_cfg: [material] 段，管 deep 抓素材的条数与限幅。
            credential_cfg: [bili_credential] 段，deep 抓素材用的 B 站凭据来源。
            asr_cfg: [asr] 段（默认 None），只有 deep 会用到：原样交给素材服务，
                字幕缺失且 enabled=true 时素材里补一段「语音转写（ASR）」，外层时限
                同步放宽；simple 与 off 完全不碰这段配置。

        Returns:
            推荐语文本；空串表示「不点评」——总开关关或 push_mode=off 时，
            消息只发标题＋链接。
        """
        if opinion_cfg is not None:
            if not bool(getattr(opinion_cfg, "recommend_enabled", True)):
                # 总开关关了：金口不开，只发标题＋链接，push_mode 也懒得看
                return ""
            mode = self._normalize_push_mode(getattr(opinion_cfg, "push_mode", None))
            if mode == "off":
                return ""
            if mode == "deep":
                deep = await self._build_deep_comment(
                    video, persona, material_cfg, credential_cfg, asr_cfg=asr_cfg,
                )
                if deep is None:
                    logger.warning("deep 素材/模型不可用，已回落 simple 浅推荐语")
                elif deep:
                    return deep
                else:
                    logger.warning("deep 点评 LLM 回包失败，改用模板文案")
                    return self._fallback_comment(video)
        return await self._build_simple_comment(video, persona)

    async def _build_simple_comment(self, video: dict[str, Any], persona: str) -> str:
        """simple 模式：让 LLM 只看标题和简介，随口写一句轻推荐。"""
        if self._model_set is None:
            return self._fallback_comment(video)

        prompt = _COMMENT_PROMPT.format(
            persona=persona,
            title=video.get("title", ""),
            description=str(video.get("description", ""))[:300],
        )

        request = create_llm_request(
            self._model_set,
            request_name="bilibili_sharer:comment",
        )
        request.add_payload(LLMPayload(ROLE.USER, Text(prompt)))

        try:
            response = await asyncio.wait_for(request.send(stream=False), timeout=30.0)
            comment = (response.message or "").strip()
            if comment:
                return comment
        except Exception as e:
            logger.warning(f"LLM 生成评论失败: {e}")

        # LLM 指望不上时，就用标题和 UP 主自己拼一句推荐
        return self._fallback_comment(video)

    async def _build_deep_comment(
        self,
        video: dict[str, Any],
        persona: str,
        material_cfg: Any,
        credential_cfg: Any,
        *,
        asr_cfg: Any = None,
    ) -> str | None:
        """deep 模式：先读素材，再对视频内容本身点评（TTL 缓存放在素材服务侧，
        多个目标推同一个视频不会重复抓）。

        返回值分三种脸色：
        - 非空串 → 点评成功，直接用；
        - None → 素材或模型不可用，调用方退回 simple 的轻推荐；
        - 空串 → 素材已就绪但 LLM 没给出可用文本，调用方直接套模板文案，不叫模型第二次。

        asr_cfg 原样转交给 build_material_markdown（要不要做语音转写由素材服务按
        [asr].enabled 自己定），本方法的外层兜底时限也跟着它放宽。
        """
        if self._model_set is None:
            return None
        link = str(video.get("url") or video.get("bvid") or "").strip()
        if not link:
            logger.warning("deep 模式：视频缺少 url/bvid，无法抓取素材")
            return None

        credential = None
        if credential_cfg is not None:
            try:
                credential = load_credential(credential_cfg)
            except Exception as e:  # noqa: BLE001
                # 凭据加载失败时，异常文本也要抹掉敏感信息再进日志：这时拿不到 Credential，
                # 就按 [bili_credential] 配置字段值来抹，文件来源的敏感值由素材侧登记表兜底
                logger.warning(
                    f"deep 模式凭据加载失败，按匿名访问: "
                    f"{_redact(f'{type(e).__name__}: {e}', credential_cfg)}"
                )
                credential = None

        # 外层时限跟着 [asr] 开关走：关闭态恰好就是 _DEEP_MATERIAL_TIMEOUT_SECS；
        # 开启态则给语音转写留足预算（见 _deep_material_timeout）
        material_timeout = _deep_material_timeout(asr_cfg)
        try:
            material = await asyncio.wait_for(
                build_material_markdown(
                    link,
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
        except BiliMaterialError as e:
            # 异常文本一律先过 material._redact 再进日志；抹两层来源（已加载的 Credential ∪
            # [bili_credential] 配置段），单层缺字段还有另一层接着
            logger.warning(f"deep 模式素材获取失败: {_redact(_redact(e, credential), credential_cfg)}")
            return None
        except asyncio.TimeoutError:
            logger.warning(f"deep 模式素材获取超时（{material_timeout:.0f}s）: {link[:80]}")
            return None
        except Exception as e:  # noqa: BLE001
            # 同上：异常文本先过 _redact，两层来源一起抹
            logger.warning(
                f"deep 模式素材获取异常: "
                f"{_redact(_redact(f'{type(e).__name__}: {e}', credential), credential_cfg)}"
            )
            return None
        body = str(material or "").strip()
        if not body:
            logger.warning("deep 模式素材为空")
            return None

        prompt = _DEEP_COMMENT_PROMPT.format(
            persona=persona,
            title=video.get("title", ""),
            description=str(video.get("description", ""))[:300],
            material=body[:_DEEP_MATERIAL_CAP],
        )
        request = create_llm_request(
            self._model_set,
            request_name="bilibili_sharer:comment_deep",
        )
        request.add_payload(LLMPayload(ROLE.USER, Text(prompt)))
        try:
            response = await asyncio.wait_for(request.send(stream=False), timeout=_DEEP_LLM_TIMEOUT_SECS)
            comment = (response.message or "").strip()
            if comment:
                return comment[:_DEEP_COMMENT_MAX_CHARS]
            logger.warning("deep 模式 LLM 返回空文本")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"deep 模式 LLM 生成点评失败: {e}")
        return ""

    @staticmethod
    def _fallback_comment(video: dict[str, Any]) -> str:
        """LLM 不营业时的模板文案：用标题、UP 主、分区拼一句推荐。"""
        title = video.get("title", "")
        author = video.get("author", "")
        category = video.get("category", "")
        short_title = title[:30] + ("…" if len(title) > 30 else "")
        if title:
            msg = f"分享一个视频《{short_title}》"
        elif category:
            msg = f"推荐一个{category}区的视频"
        else:
            msg = "来看看这个视频吧"
        if author:
            msg += f"，UP主：{author}"
        return msg + "~"

    def format_message(self, video: dict[str, Any], comment: str) -> str:
        """拼最终消息：表情＋标题、链接、推荐语；推荐语为空（总开关关或 off）时
        就只发标题和链接两行。
        """
        prefix = random.choice(_PREFIX_EMOJIS)
        title = video.get("title", "未知视频")
        url = video.get("url", "")
        lines = [f"{prefix} {title}", f"🔗 {url}"]
        if str(comment or "").strip():
            lines.append(f" {comment}")
        return "\n".join(lines)
