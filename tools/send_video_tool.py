"""bili_send_video：把 B 站视频**真正发出去**，而不只是贴个链接。

为什么要有它？以前的 Bot 在聊天里只会说「我这边只能贴链接和 BV 号，视频本体传不了」——
因为读素材的工具只负责「看」，没有一个「把文件发出去」的入口。这个工具补上那一半：
它下载视频（音视频合流）、交给平台发出去，然后删掉本地临时文件。

和被动链路的区别：被动链路是「别人发了链接 → 自动发视频」；本工具是「LLM 想主动发 → 自己调」。
开关与另外两个工具一致（都在 execute 第一句看 [chat_tools].enabled）；
体积 / 时长 / 清晰度 / ffmpeg 路径这些限制沿用 [video_send] 的配置。
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseTool

from ..config import BiliSharerConfig
from ..services.bili_downloader import BiliDownloadError, download_bili_video, send_video_file
from ..services.material import BiliMaterialError, _redact, extract_bvid, load_credential, resolve_b23_short

logger = get_logger('bili_sharer_tools')

# 聊天工具被关掉时，工具统一回这一句（与另外两个工具逐字相同）
GATE_OFF_TEXT = '聊天视频工具未启用（chat_tools.enabled=false）'


class BiliSendVideoTool(BaseTool):
    """把 B 站视频下载并发到当前会话——LLM 想「直接给你看」时用这件工具。

    下载走 services/bili_downloader（DASH 双流 + ffmpeg 合流，带体积/时长限制），
    发送走 send_video（video 段），发完即删本地临时文件，不在硬盘上留垃圾。
    """

    name: str = 'bili_send_video'
    description: str = (
        '把 B 站视频真的发出去（下载后以视频消息发出），而不只是贴链接。当用户想看视频本体、'
        '问「视频呢」、或你刚在聊天里贴了 B 站链接想顺手把视频也发出来时调用它。参数 url 可以是'
        'BV 号 / av 号 / bilibili.com 链接 / b23.tv 短链，page 指定第几个分 P（默认 1）。'
        '下载需要数秒到数十秒（看体积），失败会返回原因；不要因为怕慢就不调用——用户要的是视频本身。'
    )

    async def execute(
        self,
        url: Annotated[str, 'B站视频链接/BV号/av号/b23.tv短链'],
        page: Annotated[int, '分P序号，默认1'] = 1,
    ) -> tuple[bool, str]:
        """下载并发送这个 B 站视频。

        Args:
            url: 视频链接 / BV 号 / av 号 / b23.tv 短链（夹在分享文案里也可以）。
            page: 分 P 序号（从 1 开始，默认 1）。

        Returns:
            (成功与否, 给 LLM 看的说明)；失败时说明里带原因，不会抛异常。
        """
        cfg = cast(BiliSharerConfig, self.plugin.config)
        # 开关判定放在最前面：关着就只回固定文案，凭据和网络一个都不碰
        if not bool(cfg.chat_tools.enabled):
            logger.warning(f'bili_send_video 被调用但{GATE_OFF_TEXT}')
            return False, GATE_OFF_TEXT

        raw_url = str(url or '').strip()
        if not raw_url:
            return False, '发视频失败：没给链接或 BV 号呀'

        # 凭据先在这里读一次：一是给异常文本脱敏用，二是下载器会自己按 cfg.bili_credential
        # 再读一次——登录态影响能拿到的画质，匿名下载通常只能就近降级。
        credential: Any = None
        try:
            credential = load_credential(cfg.bili_credential)
        except Exception:  # noqa: BLE001
            credential = None

        # 先把链接认成能下载的样子：能直接认出号就不联网，认不出才当短链还原一次
        try:
            link = raw_url
            try:
                extract_bvid(link)
            except BiliMaterialError:
                link = await resolve_b23_short(
                    link, timeout=float(cfg.material.request_timeout_seconds)
                )
                extract_bvid(link)
        except Exception as exc:  # noqa: BLE001
            safe = _redact(f'{type(exc).__name__}: {exc}', credential)
            logger.warning(f'bili_send_video 链接识别失败：{safe}')
            return False, '发视频失败：没认出 B 站视频号，给我 BV 号或视频链接就好'

        # 发到哪儿？当前会话（聊天流的 stream_id 由框架提供）
        stream_id = str(self.get_current_stream_id() or "").strip()
        if not stream_id:
            return False, '发视频失败：当前会话不可用（拿不到聊天流 ID）'

        # 下载（体积/时长/清晰度限制都在下载器里，超限会抛出可读原因）。
        # 这里递**整份插件配置**而不是只递 cfg.video_send：下载器要按 cfg.bili_credential
        # 读登录凭据，登录后才能拿到配置里请求的画质（匿名常常只能拿到 480P 左右）。
        try:
            path, meta = await download_bili_video(link, page_num=int(page or 1), cfg=cfg)
        except BiliDownloadError as exc:
            return False, '发视频失败：' + _redact(str(exc), credential)
        except Exception as exc:  # noqa: BLE001
            return False, '发视频失败：' + _redact(f'{type(exc).__name__}: {exc}', credential)

        # 发送（video 段；发完即删本地临时文件）
        ok = await send_video_file(path, stream_id, '', delete_after=True)
        if not ok:
            return False, '视频下载好了，但发送没成功（详情看日志）'

        meta = meta or {}
        title = str(meta.get('title') or '').strip()
        bvid = str(meta.get('bvid') or '').strip()
        label = f'《{title}》' if title else (bvid or '')
        logger.info(f'bili_send_video 已发送：{bvid or link} -> {stream_id}')
        return True, ('视频已经发出去啦' + label).strip()