"""bilibili_sharer 的入口：框架开机时最先敲这扇门，插件在这里把人手和工具都摆好。

它只做两件事：一是报上五个常驻组件（读素材、搜视频、发视频、扫码登录、守群被动链路），
二是插件启用时把各服务依次接起来、交给调度器开始每天定时干活。
"""

from __future__ import annotations

from typing import cast

from src.app.plugin_system.base import BasePlugin, register_plugin
from src.app.plugin_system.api.log_api import get_logger

from .builder import MessageBuilder
from .components import ReactiveHandler
from .config import BiliSharerConfig
from .services.bili_downloader import cleanup_orphan_cache
from .services.fetcher import BiliFetcher
from .services.filter import VideoFilter
from .services.scheduler import SharerScheduler
from .services.state_manager import StateManager
from .tools import (
    BiliLoginTool,
    BiliSearchVideosTool,
    BiliSendVideoTool,
    BiliVideoMaterialTool,
)

logger = get_logger("bilibili_sharer")


@register_plugin
class BiliSharerPlugin(BasePlugin):
    """Bilibili 分享插件本体：框架认名字（plugin_name）、看清单（组件与配置类），
    加载时接线、卸载时收线。真正做事的是它请出来的那些组件与服务。
    """

    plugin_name = "bilibili_sharer"

    configs: list[type] = [BiliSharerConfig]
    dependent_components: list[str] = []

    def get_components(self) -> list[type]:
        # 五个组件一直都注册，不做「开关开着才注册」那种条件注册：
        # 要不要真做事，全在 handler 与工具自己内部判——清单必须与 manifest.json 的 include 一致。
        return [
            BiliVideoMaterialTool,
            BiliSearchVideosTool,
            BiliSendVideoTool,
            BiliLoginTool,
            ReactiveHandler,
        ]

    async def on_plugin_loaded(self) -> None:
        cfg: BiliSharerConfig = cast(BiliSharerConfig, self.config)
        # 先扫缓存、再判启用：只要插件被加载过，这里就一定扫一次。
        # 为什么要排在最前面？聊天工具的门是 chat_tools.enabled 管的，和插件总开关无关——
        # 整体关掉时读素材的工具仍可能往 data/bilibili_sharer/cache 落文件，而 scheduler
        # 只在启用后才启动；把清扫挂在那条链路上，关着的这段时间就永远没人收拾残局。
        # 清扫本身很规矩：只看本插件缓存目录顶层、按 mtime 放过正在下载的文件、只 log 不抛。
        cleanup_orphan_cache()
        if not cfg.plugin.enabled:
            logger.info("插件未启用，跳过初始化")
            return

        # 把推送链路的各位伙伴依次请出来：记账本 → 抓取 → 筛选 → 排版。
        self._state_manager = StateManager()
        await self._state_manager.load()

        self._fetcher = BiliFetcher(cfg)
        self._filter = VideoFilter(cfg.filter)
        self._builder = MessageBuilder(cfg.builder)

        # 最后把调度器请出来，两个后台循环从这一刻开始转。
        # 少了这一段，插件会加载成功、日志也好看，但 fetch/send 永不启动——
        # 只在群里被动发视频，定时推送静悄悄不干活（v1.2.0/v1.2.2 发布件就栽在这里）。
        self._scheduler = SharerScheduler(
            plugin=self,
            config=cfg,
            state_manager=self._state_manager,
            fetcher=self._fetcher,
            video_filter=self._filter,
            builder=self._builder,
        )
        await self._scheduler.start()
        logger.info("Bilibili sharer 插件加载完成")

    async def on_plugin_unloaded(self) -> None:
        # 卸载收尾：把定时调度停掉。先 hasattr 问一句，是因为插件从没启用过时它压根没被创建。
        if hasattr(self, "_scheduler"):
            await self._scheduler.stop()
        logger.info("Bilibili sharer 插件已卸载")
