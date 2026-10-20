"""bilibili_sharer 的原生 LLM 工具包：递给聊天用的四件 B 站小工具。

一件读素材（bili_video_material：字幕 / 弹幕 / 热门评论），一件搜视频
（bili_search_videos：给关键词，拿回一份结构化视频列表），一件发视频
（bili_send_video：下载后真的把文件发出去），还有一件扫码登录
（bili_login：手机 B 站扫一下，把登录态交给插件，字幕和画质都靠它）。

前两件是原先 BiliLearn MCP（xiaoyaya191/bilibili_learning_bot,
© 2025 XingYe contributors, MIT）那两个工具搬到本插件里来的：抓与搜的实活儿
由 services/material.py 承担，进程内直接调用——不用再养 MCP 子进程，也没有协议流。

组件名固定为 bili_video_material / bili_search_videos / bili_send_video / bili_login，
manifest.json 的 include 认的就是它们。
"""

from __future__ import annotations

from .login_tool import BiliLoginTool
from .material_tool import BiliVideoMaterialTool
from .search_tool import BiliSearchVideosTool
from .send_video_tool import BiliSendVideoTool

__all__ = [
    "BiliLoginTool",
    "BiliSearchVideosTool",
    "BiliSendVideoTool",
    "BiliVideoMaterialTool",
]