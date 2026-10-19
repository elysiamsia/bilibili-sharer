"""bili_search_videos：帮 LLM 在 B 站搜一圈视频，端回来一份结构化清单。

想找资料、想找选题、想知道某个关键词下有什么可看的，就喊它。
搜到的每条都带标题 / BV / UP 主 / 播放量 / 时长 / 分区 / 简介 / 链接。

它是从 BiliLearn MCP 的 bili_search_videos 搬进来的：描述与参数语义移植自那边，
搜索的实活儿由 services/material.py 的 search_videos_markdown 承担。

MIT 归属：description 语义移植自 xiaoyaya191/bilibili_learning_bot,
© 2025 XingYe contributors, MIT。
"""

from __future__ import annotations

from typing import Annotated, cast

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseTool

from ..config import BiliSharerConfig
from ..services.material import _redact, search_videos_markdown

logger = get_logger("bili_sharer_tools")

# 聊天工具被关掉时，工具统一回这一句（固定文案；test_chat_tools_gate.py 按字面断言它）。
GATE_OFF_TEXT = "聊天视频工具未启用（chat_tools.enabled=false）"


class BiliSearchVideosTool(BaseTool):
    """按关键词搜 B 站视频：进去一个词，出来一份可以逐条点开看的清单。
    """

    name: str = "bili_search_videos"
    description: str = (
        "按关键词搜索 B 站视频，返回结构化列表（标题 / BV / UP 主 / 播放量 / 时长 / 分区 / 简介 / 链接）。"
        "当聊天里需要找视频（找资料、找选题、想看某个关键词下有哪些视频）时调用。"
    )

    async def execute(
        self,
        query: Annotated[str, "搜索关键词"],
        limit: Annotated[int, "最多返回条数，默认8"] = 8,
    ) -> tuple[bool, str]:
        """去 B 站搜一把，把结果整理成 Markdown 清单交回去。

        Args:
            query: 搜索关键词。
            limit: 想要几条；配置 search_limit 是天花板（只能收紧、不能放宽），
                实际请求条数 = min(limit, cfg.material.search_limit)。

        Returns:
            (成功与否, 文本)：成功时是清单 Markdown；失败时 (False, 给 LLM 看的说明)。
        """
        cfg = cast(BiliSharerConfig, self.plugin.config)
        # 开关就在这儿判：两个工具始终注册，关着时不消失、只是不干活。
        # 关闭时直接回那句固定提示并记日志，其余东西一个都不碰。
        if not bool(cfg.chat_tools.enabled):
            logger.warning(f"bili_search_videos 被调用但{GATE_OFF_TEXT}")
            return False, GATE_OFF_TEXT

        try:
            # int(limit) 特意放在 try 里：LLM 传回「八条」这种非数值时，好好回一条失败说明，不抛。
            requested = int(limit)
            cap = int(cfg.material.search_limit)
            # 条数封顶：参数只能往少了要，不能突破配置上限——聊天上下文是很宝贵的。
            effective = min(requested, cap) if cap > 0 else requested
            markdown = await search_videos_markdown(str(query or ""), effective)
        except Exception as exc:  # noqa: BLE001
            # 异常文本先过素材模块的脱敏入口：搜索这条路不建 Credential，就拿配置字段值去抹。
            safe_exc = _redact(exc, cfg.bili_credential)
            logger.warning(f"搜索失败：{type(exc).__name__}: {safe_exc}")
            return False, f"搜索失败：{safe_exc}"
        return True, markdown
