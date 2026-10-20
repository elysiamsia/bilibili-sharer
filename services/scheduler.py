"""后台调度器 — 两个各自上班的小钟：fetch 负责收视频，send 负责寄消息。

fetch_loop 按点叫抓取器把视频收进 Raw Pool；send_loop 按点查每个目标的闹钟，
到点就挑一条发出去。推荐语怎么写由 builder.build_comment 决定，这里只把
[opinion]/[material]/[bili_credential]/[asr] 配置段原样递过去（缺段就当 None）。

video_send.push_enabled 开：链接消息发完后自己动手下载视频文件再发一次，
并明确跳过合成事件（免得 parser 重复下载）；关（默认）：维持现状，
链接＋合成事件交给 parser 处理。自发视频自己的下载时限（_PUSH_DOWNLOAD_TIMEOUT）与 ASR 无关、不会放宽。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from src.app.plugin_system.api.event_api import publish_event
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.api.stream_api import get_or_create_stream
from src.core.components.types import EventType
from src.core.models.message import Message, MessageType
from src.kernel.concurrency import get_task_manager

from ..config import BiliSharerConfig
from ..builder import MessageBuilder
from .bili_downloader import BiliDownloadError, download_bili_video, send_video_file
from .fetcher import BiliFetcher
from .filter import VideoFilter
from .persona import compose_persona
from .state_manager import StateManager

logger = get_logger("bili_sharer_scheduler")

_SEND_INITIAL_DELAY_SECS = 30  # 插件启动后先等 30 秒，让首轮抓取把库存备好
_MAX_JUDGE_VIDEOS = 30  # 一轮最多问模型 30 个视频，别把整个池子端上去
_PUSH_DOWNLOAD_TIMEOUT = 900.0  # 「自发视频文件」下载的整体兜底超时（与被动链路口径一致）


def _get_target_key(target: Any) -> str:
    """把目标的 type + id 拼成一把钥匙，和 state_manager 记账的口径一致。"""
    return f"{target.type}_{target.id}"


class SharerScheduler:
    """调度器本器：照看 fetch 和 send 两个后台循环，让它们各忙各的、互不打扰。"""

    def __init__(
        self,
        plugin: Any,
        config: BiliSharerConfig,
        state_manager: StateManager,
        fetcher: BiliFetcher,
        video_filter: VideoFilter,
        builder: MessageBuilder,
    ) -> None:
        self._cfg = config
        self._state = state_manager
        self._fetcher = fetcher
        self._filter = video_filter
        self._builder = builder
        self._running = False
        self._check_count = 0  # 轮询计数器，用于日志降频
        self._failed_sends: dict[str, set[str]] = {}  # 目标 → 已失败的 bvid 集合
        self._warned_empty_targets: set[str] = set()  # 已发过空 ID 警告的目标

    async def start(self) -> None:
        """启动两个循环；已经在跑就什么都不做，不会雇出第二对。"""
        if self._running:
            return
        self._running = True

        tm = get_task_manager()
        tm.create_task(self._fetch_loop(), name="bilibili_sharer:fetch")
        tm.create_task(self._send_loop(), name="bilibili_sharer:send")
        logger.info("调度器已启动")

    async def stop(self) -> None:
        """停止两个循环：把 _running 开关拨灭，循环跑完手上一轮会自己收工。"""
        self._running = False
        logger.info("调度器已停止")

    # ── Fetch Loop ──

    async def _fetch_loop(self) -> None:
        """定时抓取循环：按 fetch_interval_minutes 一轮一轮地抓，永不打盹。"""
        interval = self._cfg.fetcher.fetch_interval_minutes * 60
        # 插件刚上线不等满一个间隔，先抓一批热身
        await self._do_fetch()

        while self._running:
            await asyncio.sleep(interval)
            await self._do_fetch()

    async def _do_fetch(self) -> None:
        """一轮 = 抓取 → 硬性过滤（播放量/屏蔽分区/屏蔽词）→ 活下来的进池。

        任何异常只在这里记日志、不外传——免得一次意外就掀翻定时循环。
        """
        try:
            raw_videos = await self._fetcher.fetch()
            if not raw_videos:
                logger.info("本次抓取无视频")
                return

            filtered = self._fetcher.apply_hard_filters(
                raw_videos,
                min_views=self._cfg.fetcher.min_views,
                blocked_categories=list(self._cfg.fetcher.blocked_categories),
                blocked_keywords=list(self._cfg.fetcher.blocked_keywords),
            )

            if filtered:
                await self._state.add_to_pool(filtered, max_size=self._cfg.fetcher.max_raw_pool_size)
            else:
                logger.info("硬性过滤后无剩余视频")
        except Exception as e:
            logger.error(f"抓取任务异常: {e}")

    # ── Send Loop ──

    async def _send_loop(self) -> None:
        """定时检查各目标的送达时间到没到，每隔 check_interval_seconds 查一轮。"""
        # 先等一小会儿，让 fetch 那边把库存备好再开始派送
        logger.info(f"send_loop 启动，等待 {_SEND_INITIAL_DELAY_SECS}s 后开始检查")
        await asyncio.sleep(_SEND_INITIAL_DELAY_SECS)
        logger.info("send_loop 开始检查各目标定时器")

        while self._running:
            try:
                await self._check_targets()
            except Exception as e:
                logger.error(f"发送检查异常: {e}", exc_info=True)
            await asyncio.sleep(self._cfg.sender.check_interval_seconds)

    async def _check_targets(self) -> None:
        """挨个看目标的闹钟：没到点就跳过；到点（或首轮/重启）就送去发送，
        完事再拨好下一次。"""
        now = int(time.time())
        targets = list(self._cfg.sender.targets)
        self._check_count += 1
        # 摘要日志每 10 轮才报一次，不做碎碎念
        if self._check_count % 10 == 1:
            logger.info(f"_check_targets 开始: {len(targets)} 个目标, now={now}")

        for target in targets:
            target_key = _get_target_key(target)

            # 目标 ID 没填等于没有地址：跳过，且每种 type 只提醒一次，不刷屏
            if not target.id or not target.id.strip():
                if target.type not in self._warned_empty_targets:
                    self._warned_empty_targets.add(target.type)
                    logger.warning(f"目标 type={target.type} 的 ID 为空，已跳过该目标")
                continue

            state = self._state.get_target_state(target_key)

            # 首次运行 / 刚重启：闹钟拨成「现在」，本轮直接派送一份见面礼
            if state["last_send_time"] <= 0 or state["next_send_time"] <= 0:
                state["next_send_time"] = now
                logger.info(f"目标 {target_key} 首次/重启触发发送")
                await self._state.save_states()
                # 这里没有 continue：让它顺势落进下面的到期发送分支

            if now < state["next_send_time"]:
                # 等待是常态：每 10 轮 INFO 一次即可，别每轮都喊「还没到点」
                if self._check_count % 10 == 1:
                    logger.info(
                        f"目标 {target_key}: 等待中 last_send={state['last_send_time']}"
                        f" next_send={state['next_send_time']}",
                    )
                continue

            # 闹钟响了，派送交给 _try_send 张罗
            await self._try_send(target, target_key, state)
            # 无论这轮实际发没发出去，都把下一次闹钟拨好
            state["next_send_time"] = now + int(target.send_interval_hours * 3600)
            state["last_send_time"] = now
            await self._state.save_states()

    async def _try_send(self, target: Any, target_key: str, state: dict) -> None:
        """给这个目标挑一条视频并发送。

        流程：池子里取该目标「还没收到过」的视频 → 剔除之前发送失败的老冤家 →
        前 _MAX_JUDGE_VIDEOS 个交给批量 LLM 判断 → 池序里第一个命中人设的中选 →
        生成推荐语、发出、记进「已发送」历史。没库存或没命中就安静返回。
        """
        pool = self._state.get_all_videos()
        unwatched = self._state.get_unwatched_videos(target_key, pool)

        # 之前发失败过的视频不再重试，免得每轮都栽在同一个坑里
        failed_bvids = self._failed_sends.get(target_key, set())
        unwatched = [v for v in unwatched if v["bvid"] not in failed_bvids]

        if not unwatched:
            logger.info(f"目标 {target_key} 无未看过的视频（含已过滤的失败视频），跳过本次发送")
            return

        # 这个目标的人设与兴趣：自己填了就用，没填就继承全局那套。
        # 全局那份由 compose_persona 组装——七个字段默认复用 bot 的全局人格（config/core.toml
        # [personality]），[persona] 填了哪项就覆盖哪项，persona 字段作额外补充缀在后面。
        persona = (target.persona or compose_persona(self._cfg.persona)).strip()
        interests = list(target.interests) if target.interests else list(self._cfg.persona.interests)

        # 批量问模型：每 batch_size 个打包一次调用（cfg.filter.batch_size），按池序取第一个命中的
        candidates = unwatched[:_MAX_JUDGE_VIDEOS]
        batch_size = max(1, int(getattr(self._cfg.filter, "batch_size", 10) or 10))
        calls = (len(candidates) + batch_size - 1) // batch_size
        logger.info(
            f"目标 {target_key} 批量判断 {len(candidates)} 个（{calls} 次调用），"
            f"批大小 {batch_size}（池中 {len(pool)}，未看 {len(unwatched)}）"
        )
        judged = await self._filter.judge_batch(
            candidates, persona, interests,
            blocked_keywords=list(self._cfg.fetcher.blocked_keywords),
            batch_size=batch_size,
        )

        selected: dict[str, Any] | None = None
        for video, matches, reason, confidence in judged:
            if matches:
                selected = video
                logger.info(
                    f"目标 {target_key} ✅ 批量判断 {len(candidates)} 个（{calls} 次调用），"
                    f"命中：bvid={video.get('bvid')} confidence={confidence:.2f} reason={reason}"
                )
                break

        if selected is None:
            logger.info(f"目标 {target_key} 无匹配视频，跳过本次发送")
            return

        # 推荐语怎么写由 builder 决定，这里只管写出来、发出去
        try:
            comment = await self._build_push_comment(selected, persona)
            message = self._builder.format_message(selected, comment)
            video_url = selected.get("url", "")
            # push_enabled 开 → 我们自己下载并自发视频文件，并明确跳过合成事件（parser 不掺和）；
            # 关（默认）→ 维持现状：发链接，靠合成事件让 parser 去下载
            push_video = bool(getattr(getattr(self._cfg, "video_send", None), "push_enabled", False))
            if push_video:
                # 开：关掉合成事件，免得我们和 parser 各下一份、对方收到双份
                delivered = await self._send_to_target(
                    target, message, video_url, enable_parser_event=False,
                )
            else:
                # 关（默认）：照老样子调用，不传任何新关键字参数
                delivered = await self._send_to_target(target, message, video_url)
            if delivered is None:
                # 文字压根没送出去（stream 建不出来），这条视频不能算「已看过」——
                # 否则它会带着「已发送」的标记永远沉默下去。记进本目标的失败名单，
                # 等通道恢复、下一个视频发送成功时整个名单会自动清空，它还有机会再来。
                bvid = selected.get("bvid", "")
                if bvid:
                    self._failed_sends.setdefault(target_key, set()).add(bvid)
                logger.warning(
                    f"向目标 {target_key} 发送失败（stream 不可用），本轮不标记 bvid={bvid} 为已看"
                )
                return
            await self._state.mark_as_sent(target_key, selected["bvid"])
            # 这次发成功了——把这个目标的失败黑名单整个清空，既往不咎
            self._failed_sends.pop(target_key, None)
            logger.info(f"已向目标 {target_key} 发送视频 bvid={selected.get('bvid')}")
            if push_video:
                # _send_push_video 的异常自己消化：视频文件发失败也不影响「链接确实送到了」，
                # 不该因此把这个视频拉进失败黑名单
                await self._send_push_video(video_url, delivered)
        except Exception as e:
            logger.error(f"向目标 {target_key} 发送失败（将跳过该视频）: {e}")
            # 把这个视频记进该目标的失败名单，下轮绕开这个坑
            bvid = selected.get("bvid", "")
            if bvid:
                self._failed_sends.setdefault(target_key, set()).add(bvid)

    async def _build_push_comment(self, video: dict[str, Any], persona: str) -> str:
        """按 [opinion] 段的三态把活儿交给 builder。

        配置里没有 [opinion] 段时（单测 stub / 异构配置）保持旧的两参调用形态，
        行为等同 simple。

        [asr] 段永远原样递过去（用 getattr 兜底、缺了就是 None）：要不要真的做语音
        转写由 services/material.py 按 enabled 判定——缺段或 enabled=false 时素材不会
        多出 ASR 段，deep 的兜底时限也停在基础值，一切如常。
        """
        opinion_cfg = getattr(self._cfg, "opinion", None)
        if opinion_cfg is None:
            return await self._builder.build_comment(video, persona)
        return await self._builder.build_comment(
            video, persona,
            opinion_cfg=opinion_cfg,
            material_cfg=getattr(self._cfg, "material", None),
            credential_cfg=getattr(self._cfg, "bili_credential", None),
            asr_cfg=getattr(self._cfg, "asr", None),
        )

    async def _send_to_target(
        self,
        target: Any,
        message: str,
        video_url: str,
        *,
        enable_parser_event: bool = True,
    ) -> tuple[str, str] | None:
        """把这条消息送到目标（群聊或私聊）。

        enable_parser_event=True（默认）→ 文字发完后发布一个合成消息事件，让
        parser 认出链接、自动去下载视频（现状保留）；False → 不发布，
        push_enabled 自发视频时用，免得两边都下载。

        Returns:
            发送成功返回 (stream_id, platform)——platform 就是本方法解析流时用的
            target.platform，后面自发视频直接复用这个值，不必再解析一遍目标；
            stream 拿不到时返回 None。
        """
        if target.type == "group":
            stream = await get_or_create_stream(
                platform=target.platform,
                group_id=str(target.id),
                chat_type="group",
            )
        else:
            stream = await get_or_create_stream(
                platform=target.platform,
                user_id=str(target.id),
                chat_type="private",
            )

        if not stream or not stream.stream_id:
            logger.error(f"获取目标 {target.type}_{target.id} 的 stream 失败")
            return None

        # 第一步：先把文字消息发出去
        await send_text(message, stream_id=stream.stream_id)

        # 第二步（默认开）：假装「收到了一条带链接的消息」，让 parser 自动识别并下载视频
        if enable_parser_event:
            try:
                fake_message = Message(
                    message_id=f"bili_sharer_{int(time.time())}",
                    content=video_url,
                    processed_plain_text=video_url,
                    message_type=MessageType.TEXT,
                    sender_id="",
                    sender_name="",
                    platform=target.platform,
                    chat_type=target.type,
                    stream_id=stream.stream_id,
                )
                await publish_event(EventType.ON_MESSAGE_RECEIVED, {
                    "message": fake_message,
                })
                logger.info(f"已触发 parser 解析视频: url={video_url}")
            except Exception as e:
                logger.error(f"触发 parser 失败: {e}")

        return stream.stream_id, target.platform

    async def _send_push_video(self, video_url: str, delivered: tuple[str, str] | None) -> None:
        """push_enabled 开：链接发出去之后，用本插件自己的下载器拿下视频文件再直发。

        - stream_id / platform 复用 _send_to_target 的返回值，不重复解析目标；
        - 自己下载的文件发完就删（delete_after=True）——只清理自己带来的东西，
          parser 名下的缓存文件一个都不碰；
        - 任何失败只记日志、不上抛：链接反正送到了，不该因文件没发出去就记视频的黑账。
        """
        if delivered is None:
            logger.warning("推送自发视频跳过：stream 不可用，链接消息未发出")
            return
        if not video_url:
            logger.warning("推送自发视频跳过：视频链接为空")
            return
        stream_id, platform = delivered
        try:
            path, meta = await asyncio.wait_for(
                download_bili_video(video_url, page_num=1, cfg=self._cfg),
                timeout=_PUSH_DOWNLOAD_TIMEOUT,
            )
        except BiliDownloadError as e:
            logger.warning(f"推送自发视频下载失败（已只发链接）: {e}")
            return
        except asyncio.TimeoutError:
            logger.warning(
                f"推送自发视频下载超时（{_PUSH_DOWNLOAD_TIMEOUT:.0f}s，已只发链接）: {video_url[:80]}"
            )
            return
        except Exception as e:  # noqa: BLE001
            logger.error(f"推送自发视频下载异常（已只发链接）: {type(e).__name__}: {e}", exc_info=True)
            return
        info = meta if isinstance(meta, dict) else {}
        logger.info(
            f"推送视频下载完成 bvid={info.get('bvid', '')} "
            f"size={info.get('size', '?')}: {path}"
        )
        try:
            sent = await send_video_file(Path(path), stream_id, platform, delete_after=True)
        except Exception as e:  # noqa: BLE001
            logger.error(f"推送自发视频文件异常: {type(e).__name__}: {e}", exc_info=True)
            return
        if not sent:
            # send_video_file 内部已有日志，这里再补一句调用侧的动静，方便两头对上
            logger.warning(
                f"推送自发视频发送失败（send_video_file=False，详见服务侧日志）：{stream_id}"
            )
