"""LLM 人设过滤 — 把目标的人设、兴趣和一批视频信息打包问模型：「哪些它会喜欢？」
每批只调用一次 LLM，比一条条单独问省 token，答案也更好玩。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from src.app.plugin_system.api.llm_api import create_llm_request
from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMPayload, ROLE, Text

from .llm_utils import load_model_set

logger = get_logger("bili_sharer_filter")

_BATCH_PROMPT_TEMPLATE = """## 任务
判断下列 Bilibili 视频是否符合 Bot 的人设兴趣。

## Bot 人设
{persona}
兴趣方向：{interests}

## 待判断视频列表（共 {count} 个）
{videos}

## 屏蔽规则（最高优先级）
{blocked_keywords}

## 输出格式
只输出一个 JSON 数组，不要包含其他内容；数组长度必须等于 {count}，index 从 1 开始（与上方列表编号一一对应）：
[{{"index": 1, "matches": true/false, "reason": "一句话原因"}}]"""

_VIDEO_PROMPT_TEMPLATE = """{index}. 标题：{title}
   描述：{description}
   分区：{category}
   标签：{tags}"""

# 匹配 markdown 的「三连反引号代码围栏」。反引号用 chr(96) 现场拼出来，源码里就不必出现连续反引号
_FENCE_RE = re.compile(chr(96) * 3 + r"(?:json)?\s*([\s\S]*?)" + chr(96) * 3)

_MAX_DESCRIPTION_CHARS = 120  # 描述只留前 120 字，别让批量 prompt 虚胖
_BATCH_TIMEOUT_SECS = 60.0  # 等一批回话最多 60 秒，超时就整批改走关键词匹配
_MAX_CONSECUTIVE_FAILURES = 5  # 连续失败攒到 5 次：模型大概不在状态，全局降级用关键词找
_TRUE_TOKENS = {"true", "yes", "1", "是", "符合", "命中", "匹配"}
_FALSE_TOKENS = {"false", "no", "0", "否", "不符合", "未命中", "不匹配"}
# 批量判断解析失败时给玩家的提示：把几种可能都数一遍、不武断下结论；认出旧版两行格式时调用处会加强前缀
_SYSTEM_PROMPT_HINT = (
    "若 [filter].system_prompt 仍是旧版两行文本格式（匹配: 是/否 + 理由: 一句话原因），"
    "请更新为 JSON 数组版：要求模型只输出 [{\"index\": 1, \"matches\": true/false, \"reason\": \"一句话原因\"}]；"
    "其他常见原因：max_tokens 偏小导致响应被截断、JSON 不合法（单引号/尾逗号/多余文字）。"
)


def _coerce_bool(value: Any) -> bool | None:
    """把模型写的 true/false、"是"、1 这些花样统一成 Python 的 bool；认不出来就返回 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    return None


def _coerce_index(value: Any) -> int | None:
    """把模型写的 index 规整成 int；缺失或写脏返回 None，调用方会按位置补上。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        return int(value.strip())
    return None


def _looks_like_legacy_reply(text: Any) -> bool:
    """粗粗认一下回复是不是旧版两行文本格式（带「匹配」「理由」），只为把日志提示说得更准，不参与判断。"""
    raw = str(text or "")
    return "匹配" in raw and ("理由" in raw or "原因" in raw)


def _build_batch_prompt(
    videos: list[dict[str, Any]],
    persona: str,
    interests: list[str],
    blocked_keywords: list[str] | None = None,
) -> str:
    """拼批量判断用的用户 prompt——纯函数，不动网络不花钱。

    编号契约：视频列表按 1-based 编号（`1. 标题：…`），并要求模型输出的 index
    同样从 1 起、一一对应——0/1 混着用会让整批对不上号、解析全灭。
    描述只截前 _MAX_DESCRIPTION_CHARS 字；屏蔽词空着就写「（无）」。
    """
    items: list[str] = []
    for index, video in enumerate(videos, start=1):
        tags = video.get("tags", [])
        if isinstance(tags, list):
            tags_str = "、".join(str(t) for t in tags if t)
        else:
            tags_str = str(tags)
        items.append(_VIDEO_PROMPT_TEMPLATE.format(
            index=index,
            title=video.get("title", ""),
            description=str(video.get("description", ""))[:_MAX_DESCRIPTION_CHARS],
            category=video.get("category", ""),
            tags=tags_str or "（无）",
        ))
    blocked = [kw for kw in (blocked_keywords or []) if kw]
    return _BATCH_PROMPT_TEMPLATE.format(
        persona=persona,
        interests="、".join(interests),
        count=len(videos),
        videos="\n".join(items),
        blocked_keywords="、".join(blocked) if blocked else "（无）",
    )


def _parse_batch_response(text: str, expected_n: int) -> list[dict[str, Any]] | None:
    """把模型的批量回答翻译成规整结果——纯函数。

    Args:
        text: LLM 原始回话；容忍被三连反引号 json 代码块包着、也容忍前后带解说。
        expected_n: 期望条数（= 本批视频数）。

    Returns:
        按视频顺序排好的「{"index": 0-based, "matches": bool, "reason": str}」列表；
        非 JSON / 非数组 / 条数不齐 / 元素不是对象 / 缺 matches / index 重复或越界——
        任一条中招就返回 None，调用方会把整批改走关键词匹配。

    关于 index：契约要求 1-based；解析器很宽容——整体 0-based、整体 1-based（全是
    1..N 且不含 0 → 整体左移一位）、乱序、缺 index（按位置补）都照单全收，
    最后统一成 0-based 并排好序。
    """
    if not isinstance(text, str) or not text.strip() or expected_n <= 0:
        return None

    raw = text.strip()
    fence = _FENCE_RE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    else:
        # 没有代码块？那就掐头去尾抠出最外层 [...]，模型的碎碎念随它去
        start, end = raw.find("["), raw.rfind("]")
        if start != -1 and end > start:
            raw = raw[start:end + 1]

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, list) or len(data) != expected_n:
        return None

    entries: list[tuple[int | None, dict[str, Any]]] = []
    for item in data:
        if not isinstance(item, dict):
            return None
        matches = _coerce_bool(item.get("matches", item.get("match", None)))
        if matches is None:
            return None
        reason = str(item.get("reason", item.get("原因", item.get("理由", ""))) or "").strip() or "无"
        entries.append((_coerce_index(item.get("index", None)), {"matches": matches, "reason": reason}))

    explicit = [idx for idx, _ in entries]
    # 若每条都显式给了 index、没有 0、全落在 [1, expected_n] → 当它是 1-based，整体左移一格
    shift = 1 if all(i is not None and 1 <= i <= expected_n for i in explicit) and 0 not in explicit else 0
    normalized = [idx - shift if idx is not None else pos for pos, (idx, _) in enumerate(entries)]
    if sorted(normalized) != list(range(expected_n)):
        return None

    result: list[dict[str, Any]] = [{} for _ in range(expected_n)]
    for idx, (_, payload) in zip(normalized, entries):
        result[idx] = {"index": idx, **payload}
    return result


class VideoFilter:
    """靠 LLM 的个性化视频过滤器：批量判断每条视频合不合目标的人设与兴趣。"""

    def __init__(self, cfg: Any = None) -> None:
        # 模型名写错、或 model.toml 里删掉了，都不能拖垮整个插件——
        # 那就退化成「不过滤」：judge_batch() 一律放行，交给下游硬规则与发送流程把关。
        self._llm_disabled = False
        self._model_set: Any = None
        if cfg is not None:
            self._model_set = load_model_set(cfg, label="过滤", consequence="已跳过 LLM 过滤")
            if self._model_set is None:
                self._llm_disabled = True
            self._system_prompt = cfg.system_prompt
        else:
            self._system_prompt = "你是一个精确的内容审核助手。请严格按照JSON格式输出判断结果。"
        self._consecutive_failures = 0

    @staticmethod
    def _keyword_match(
        video: dict[str, Any],
        interests: list[str],
        blocked_keywords: list[str] | None = None,
    ) -> tuple[bool, str]:
        """纯关键词回退：LLM 不在状态时用——标题/描述/分区/tags 揉成一团，先查屏蔽词再查兴趣。"""
        haystack = " ".join(filter(None, [
            video.get("title", ""),
            video.get("description", "")[:200],
            video.get("category", ""),
            *([str(t) for t in video.get("tags", []) if isinstance(t, str)]),
        ])).lower()

        blocked = set(kw.strip().lower() for kw in (blocked_keywords or []) if kw and kw.strip())
        for kw in blocked:
            if kw in haystack:
                return False, f"含屏蔽词: {kw}"

        interest_matched = []
        for interest in interests:
            if interest.lower() in haystack:
                interest_matched.append(interest)
        if interest_matched:
            return True, f"关键词匹配: {', '.join(interest_matched)}"
        return False, "无关键词匹配"

    def _keyword_results(
        self,
        videos: list[dict[str, Any]],
        interests: list[str],
        blocked_keywords: list[str],
    ) -> list[tuple[dict[str, Any], bool, str, float]]:
        """整批改走关键词回退，输出顺序与入参严格对齐。"""
        results: list[tuple[dict[str, Any], bool, str, float]] = []
        for video in videos:
            matches, reason = self._keyword_match(video, interests, blocked_keywords)
            results.append((video, matches, f"[回退] {reason}", 0.5))
        return results

    async def judge_batch(
        self,
        videos: list[dict[str, Any]],
        persona: str,
        interests: list[str],
        blocked_keywords: list[str] | None = None,
        batch_size: int = 10,
    ) -> list[tuple[dict[str, Any], bool, str, float]]:
        """批量判断视频合不合人设兴趣：每批 ≤batch_size 个，一批一次 LLM 调用。

        Args:
            videos: 待判断的视频；顺序即优先级，取多少由调用方截断。
            persona: Bot 的人设描述。
            interests: 兴趣方向列表。
            blocked_keywords: 屏蔽词列表，以最高优先级规则的身份写进 prompt。
            batch_size: 每批装几个视频（来自 cfg.filter.batch_size）。

        Returns:
            「(video, matches, reason, confidence)」列表，顺序与入参一致。
            模型不可用 → 一律放行（confidence 0.0）；
            某批调用失败/解析失败 → 该批整批改走关键词回退，并记一笔连续失败；
            连续失败攒到 _MAX_CONSECUTIVE_FAILURES → 后续批次全部降级为关键词回退（confidence 0.5）。
        """
        if not videos:
            return []
        blocked = [kw for kw in (blocked_keywords or []) if kw]
        size = max(1, int(batch_size or 1))  # 防御：配置再脏，每批也至少装 1 个视频
        results: list[tuple[dict[str, Any], bool, str, float]] = []
        degrade_hint_logged = False  # 降级警告一次调用只喊一遍，不每批都吼

        for start in range(0, len(videos), size):
            chunk = videos[start:start + size]
            # 模型不营业：不判断、全放行（发送侧自有减速与去重把关）
            if self._llm_disabled:
                results.extend((v, True, "[跳过] LLM 过滤未启用", 0.0) for v in chunk)
                continue
            # 连续失败触到红线：不再浪费模型，直接改走关键词回退
            if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                if not degrade_hint_logged:
                    logger.warning(
                        f"LLM 过滤连续失败已达 {self._consecutive_failures} 次，"
                        f"本次起全局降级为关键词匹配（不再调用 LLM）"
                    )
                    logger.warning(_SYSTEM_PROMPT_HINT)
                    degrade_hint_logged = True
                results.extend(self._keyword_results(chunk, interests, blocked))
                continue
            results.extend(await self._judge_chunk(chunk, persona, interests, blocked))
        return results

    async def _judge_chunk(
        self,
        chunk: list[dict[str, Any]],
        persona: str,
        interests: list[str],
        blocked_keywords: list[str],
    ) -> list[tuple[dict[str, Any], bool, str, float]]:
        """判断一批视频：调用或解析出任何岔子→整批回退关键词并记一次连续失败；顺利则清零计数。"""
        prompt = _build_batch_prompt(chunk, persona, interests, blocked_keywords)
        request = create_llm_request(
            self._model_set,
            request_name="bilibili_sharer:judge",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(self._system_prompt)))
        request.add_payload(LLMPayload(ROLE.USER, Text(prompt)))

        raw: Any = ""
        try:
            response = await asyncio.wait_for(request.send(stream=False), timeout=_BATCH_TIMEOUT_SECS)
            raw = getattr(response, "message", "") or ""
            parsed = _parse_batch_response(raw, len(chunk))
        except Exception as e:
            self._consecutive_failures += 1
            logger.error(
                f"LLM 批量判断调用失败（连续{self._consecutive_failures}次），"
                f"整批回退关键词（{len(chunk)} 个）: {e}"
            )
            return self._keyword_results(chunk, interests, blocked_keywords)

        if parsed is None:
            self._consecutive_failures += 1
            logger.warning(
                f"LLM 批量判断响应解析失败（连续{self._consecutive_failures}次），"
                f"整批回退关键词（{len(chunk)} 个）: raw={str(raw)[:120]}"
            )
            if _looks_like_legacy_reply(raw):
                logger.warning("检测到 LLM 仍按旧版两行文本格式回复（匹配/理由）——" + _SYSTEM_PROMPT_HINT)
            else:
                logger.warning(_SYSTEM_PROMPT_HINT)
            return self._keyword_results(chunk, interests, blocked_keywords)

        self._consecutive_failures = 0
        results: list[tuple[dict[str, Any], bool, str, float]] = []
        for video, entry in zip(chunk, parsed):
            matches = bool(entry.get("matches", False))
            reason = str(entry.get("reason", "") or "无")
            confidence = 0.9
            logger.debug(
                f"视频判断 bvid={video.get('bvid')} matches={matches}"
                f" confidence={confidence:.2f} reason={reason}"
            )
            results.append((video, matches, reason, confidence))
        return results
