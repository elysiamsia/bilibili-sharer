"""bilibili_sharer 插件包 —— 会自己找 B 站视频、也会陪群友聊两句的那一位。

这里是插件的家门口：plugin.py 是门牌号（框架从这里认识它），config.py 写偏好清单，
services/ 在后厨干活（抓取、筛选、下载、转写、发送），components/ 守着群消息，
tools/ 是聊天时递给 LLM 使唤的两件小工具，builder.py 负责把内容排成消息。

它做的事一句话就能讲完：按配置去 B 站把视频找回来，再把视频或点评发到指定会话
（platform 默认是 qq，也就是 QQ 那边）。
"""
