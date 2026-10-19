"""bilibili_sharer 的原生 LLM 工具包：递给聊天用的两件 B 站小工具。

一件读素材（bili_video_material：字幕 / 弹幕 / 热门评论），一件搜视频
（bili_search_videos：给关键词，拿回一份结构化视频列表）。

它们是原先 BiliLearn MCP（xiaoyaya191/bilibili_learning_bot,
© 2025 XingYe contributors, MIT）那两个工具搬到本插件里来的：抓与搜的实活儿
由 services/material.py 承担，进程内直接调用——不用再养 MCP 子进程，也没有协议流。

组件名固定为 bili_video_material / bili_search_videos，manifest.json 的 include 认的就是它。
"""

from __future__ import annotations

from .material_tool import BiliVideoMaterialTool
from .search_tool import BiliSearchVideosTool
from .send_video_tool import BiliSendVideoTool

__all__ = ["BiliSearchVideosTool", "BiliSendVideoTool", "BiliVideoMaterialTool"]