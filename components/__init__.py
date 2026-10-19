"""bilibili_sharer 的组件包：住在这里的，是那位「守群」的被动链路处理器。

和 tools/ 的区别挺好玩：tools/ 是 LLM 聊天时自己点名的工具，不喊它就不来；
本目录放的是框架组件（EventHandler 那一类）——群里消息一到就会被叫醒的那种。

ReactiveHandler 不管开关有没有开都会注册，要不要动手全由它自己在内部判定；
它的组件名叫 bili_sharer_reactive，manifest.json 的 include 认领的正是这个名字。
"""

from __future__ import annotations

from .reactive_handler import ReactiveHandler

__all__ = ["ReactiveHandler"]
