"""bili_login：让主人扫个码，把 B 站登录态交给我们——字幕、高清、会员视频都靠它。

为什么不是「去填 SESSDATA」？因为大多数人根本不知道 cookie 在哪儿。这个工具
把登录这件事变成一次扫码：Bot 把二维码发到**当前私聊**，主人用手机 B 站扫一下，
状态轮询到「成功」就把凭据写进 `data/bilibili_sharer/credentials.json`。

写盘之后立刻生效：素材、点评、下载、发视频四条链路都是每次现读 `load_credential`，
不需要重启 Bot。凭据文件只含 sessdata / bili_jct / buvid3 / DedeUserID / ac_time_value
五个字段＋时间戳，绝不写进 config.toml，也不打印值。

安全默认：`[bili_credential].login_private_only = true` 时**只在私聊里工作**——
群聊里有人起哄让 Bot 登录，扫的会是**扫码那个人自己的账号**，所以默认不给群聊用。
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any, cast

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_image, send_text
from src.app.plugin_system.api.stream_api import get_stream, get_stream_info
from src.app.plugin_system.base import BaseTool

from ..config import BiliSharerConfig
from ..services.material import _redact, load_saved_credential, save_credential

logger = get_logger("bili_sharer_tools")

# 聊天工具总开关关闭时的统一文案（与其它三个工具一字不差）
GATE_OFF_TEXT = "聊天视频工具未启用（chat_tools.enabled=false）"

# 二维码有效期约 180 秒：留 150 秒给主人掏手机，超时就让他再叫一次
LOGIN_WAIT_SECONDS = 150.0
POLL_INTERVAL_SECONDS = 2.0


async def _chat_type(stream_id: str) -> str:
    """问一下这个会话是群聊还是私聊（拿不到就返回空串，由调用方决定放不放行）。"""
    try:
        info = await get_stream_info(stream_id)
        if isinstance(info, dict):
            value = info.get("chat_type") or info.get("type") or ""
            if value:
                return str(value)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"bili_login 取会话信息失败：{type(exc).__name__}: {exc}")
    try:
        stream = await get_stream(stream_id)
        return str(getattr(stream, "chat_type", "") or "")
    except Exception:  # noqa: BLE001
        return ""


class BiliLoginTool(BaseTool):
    """扫码登录 B 站：把二维码发在当前会话，轮询扫码结果并保存凭据。"""

    name: str = "bili_login"
    description: str = (
        "帮用户扫码登录 B 站（用手机 B 站 App 扫一下即可）。当用户说「登录B站」「扫码登录」"
        "「字幕怎么没有」「画质好糊」，或你发现 B 站内容需要登录态（无字幕/只有 480P/会员视频）"
        "时调用它。二维码会发在当前私聊里，用户扫码确认后凭据自动保存、立即生效、无需重启；"
        "群聊里默认不允许登录（怕扫成别人账号），需要用户私聊你。"
    )

    async def execute(
        self,
        force: Annotated[bool, "已经登录过是否仍要重新扫码（换账号时用），默认 false"] = False,
    ) -> tuple[bool, str]:
        """开始一次扫码登录。

        Args:
            force: 已登录时是否强制重新扫码（默认 false，直接回报当前登录状态）。

        Returns:
            (是否成功, 给 LLM/用户看的说明)。失败不抛异常。
        """
        cfg = cast(BiliSharerConfig, self.plugin.config)
        if not bool(cfg.chat_tools.enabled):
            logger.warning(f"bili_login 被调用但{GATE_OFF_TEXT}")
            return False, GATE_OFF_TEXT

        stream_id = str(self.get_current_stream_id() or "").strip()
        if not stream_id:
            return False, "登录失败：当前会话不可用（拿不到聊天流 ID）"

        if bool(getattr(cfg.bili_credential, "login_private_only", True)):
            chat_type = await _chat_type(stream_id)
            if chat_type in ("group", "discuss"):
                return False, "登录只在私聊里进行呀：群里扫码会把扫码那个人的账号绑给我。请私聊我发「登录B站」～"

        saved = load_saved_credential()
        if saved.get("sessdata") and not force:
            return True, "我这边已经存着 B 站登录态了。想换账号就跟我说「重新登录」（force=true）。"

        try:
            from bilibili_api import login_v2  # noqa: PLC0415（只在真要登录时引入）
        except Exception as exc:  # noqa: BLE001
            return False, f"登录失败：本机 bilibili-api 不支持扫码登录（{type(exc).__name__}: {exc}）"

        qr = login_v2.QrCodeLogin(platform=login_v2.QrCodeLoginChannel.WEB)
        try:
            await qr.generate_qrcode()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"bili_login 生成二维码失败：{type(exc).__name__}: {exc}")
            return False, f"登录失败：二维码生成不出来（{type(exc).__name__}），再试一次或检查网络"

        qr_path = await self._send_qr(qr, stream_id)
        try:
            return await self._wait_for_scan(qr, stream_id)
        finally:
            if qr_path is not None:
                with contextlib.suppress(OSError):
                    qr_path.unlink(missing_ok=True)

    async def _send_qr(self, qr: Any, stream_id: str) -> Path | None:
        """把二维码图片发到会话；发不出去就返回 None（后续仍然轮询，至少口头告诉用户）。"""
        handle = tempfile.NamedTemporaryFile(prefix="bili_login_", suffix=".png", delete=False)
        path = Path(handle.name)
        handle.close()
        try:
            picture = qr.get_qrcode_picture()
            picture.to_file(str(path))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"bili_login 二维码落盘失败：{type(exc).__name__}: {exc}")
            await send_text("二维码画不出来…换一次试试？", stream_id=stream_id)
            path.unlink(missing_ok=True)
            return None
        try:
            sent = await send_image(
                path.as_uri(), stream_id=stream_id, processed_plain_text="[B站登录二维码]"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"bili_login 二维码发送异常：{type(exc).__name__}: {exc}")
            sent = False
        if not sent:
            await send_text(
                "二维码没发出去（可能是这个会话不支持图片）。换成私聊再叫我一次吧。",
                stream_id=stream_id,
            )
        else:
            await send_text(
                "📱 用手机 B 站 App 扫一下上面的二维码，扫完在手机上点「确认登录」，我在这儿等你～",
                stream_id=stream_id,
            )
        return path

    async def _wait_for_scan(self, qr: Any, stream_id: str) -> tuple[bool, str]:
        """轮询扫码状态，成功后保存凭据并验证。"""
        login_v2_events = None
        try:
            from bilibili_api import login_v2  # noqa: PLC0415

            login_v2_events = login_v2.QrCodeLoginEvents
        except Exception:  # noqa: BLE001
            return False, "登录失败：bilibili-api 的扫码事件类型取不到"

        announced: set[str] = set()
        deadline = time.monotonic() + LOGIN_WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                state = await qr.check_state()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"bili_login 轮询异常（继续等）：{type(exc).__name__}: {exc}")
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            if state == login_v2_events.SCAN and "scan" not in announced:
                announced.add("scan")
                await send_text("看到你扫上了，等你在手机上点确认～", stream_id=stream_id)
            elif state == login_v2_events.CONF and "conf" not in announced:
                announced.add("conf")
                await send_text("确认收到，正在保存登录信息…", stream_id=stream_id)
            elif state == login_v2_events.TIMEOUT:
                return False, "二维码已经过期了，再叫我一次「登录B站」，我给你换一张。"
            elif state == login_v2_events.DONE:
                return await self._finish_login(qr, stream_id)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        return False, "等了 2 分半还没扫完，二维码已经过期啦；再叫我一次就能换新的。"

    async def _finish_login(self, qr: Any, stream_id: str) -> tuple[bool, str]:
        """登录成功：存凭据 → 试着查一下昵称 → 回话。"""
        try:
            credential = qr.get_credential()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"bili_login 取凭据失败：{type(exc).__name__}: {exc}")
            return False, "登录过了，但拿不到凭据…再试一次？"

        values = {
            "sessdata": getattr(credential, "sessdata", "") or "",
            "bili_jct": getattr(credential, "bili_jct", "") or "",
            "buvid3": getattr(credential, "buvid3", "") or "",
            "dedeuserid": getattr(credential, "dedeuserid", "") or "",
            "ac_time_value": getattr(credential, "ac_time_value", "") or "",
        }
        if not values["sessdata"]:
            return False, "登录成功了但没拿到 SESSDATA，可能是 B 站改接口了，先按失败处理。"

        try:
            path = save_credential(values)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"bili_login 保存凭据失败：{type(exc).__name__}: {exc}")
            return False, f"登录成功，但凭据写不进硬盘（{type(exc).__name__}），下次还得再扫。"

        name = await self._self_name(credential)
        logger.info(f"bili_login 登录成功并保存：{path}（用户 {name or '未知'}）")
        tip = f"登录成功啦，现在会用「{name}」的身份读 B 站～" if name else "登录成功啦，凭据已保存。"
        await send_text(tip + "字幕、清晰度这些会好起来；不用重启哦。", stream_id=stream_id)
        return True, tip

    @staticmethod
    async def _self_name(credential: Any) -> str:
        """用现在的凭据问一下自己的昵称；失败就算了（不影响登录本身）。"""
        try:
            from bilibili_api import user  # noqa: PLC0415

            info = await asyncio.wait_for(user.get_self_info(credential), timeout=10.0)
            if isinstance(info, dict):
                return str(info.get("name") or info.get("uname") or "")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"bili_login 查昵称失败（忽略）：{_redact(f'{type(exc).__name__}: {exc}', credential)}")
        return ""
