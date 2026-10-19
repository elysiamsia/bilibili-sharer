"""bili_video_material：把一个 B 站视频的「素材」读给 LLM 看。

素材就是字幕（没字幕时可按配置做语音转写）、弹幕和热门评论——LLM 光看标题猜内容，
和只看封面猜电影一样不靠谱；有了这份素材，它才谈得上真读过这个视频。

它是从 BiliLearn MCP 的 bili_video_material 搬进来的：触发语义与参数说明移植自那边，
抓取的实活儿改由本插件 services/material.py 承担（进程内直接调用，
不用起 MCP 子进程、也没有 JSON-RPC 协议流）。

参数设计有个小差别，是有意的：danmaku_limit / comment_limit / max_chars 不再让 LLM 自己填，
统一归 cfg.material 管（它一高兴填个几千条，聊天上下文就炸了）；include_timestamps
取「工具参数 OR 配置值」——配置给日常偏好，显式 True 才临时覆盖。

MIT 归属：description 触发语义与参数说明移植自 xiaoyaya191/bilibili_learning_bot,
© 2025 XingYe contributors, MIT。
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseTool

from ..config import BiliSharerConfig
from ..services.material import (
    _redact,
    BiliMaterialError,
    build_material_markdown,
    extract_bvid,
    load_credential,
    resolve_b23_short,
)

logger = get_logger("bili_sharer_tools")

# 聊天工具被关掉时，工具统一回这一句（固定文案；test_chat_tools_gate.py 按字面断言它）。
GATE_OFF_TEXT = "聊天视频工具未启用（chat_tools.enabled=false）"


def _load_credential_quiet(cfg: BiliSharerConfig) -> Any:
    """把 B 站登录凭据准备好；备不出来就当匿名访客，聊天工具照常能跑。

    输入：插件配置。做了什么：调 load_credential 造凭据对象；出错只记一条 warning
    （文案已抹掉凭据值），然后返回 None 表示「匿名访问」——绝不把异常递给 LLM 那边。
    """
    try:
        return load_credential(cfg.bili_credential)
    except Exception as exc:  # noqa: BLE001
        # 多一道保险：异常文本也过素材模块的脱敏入口（拿不到 Credential 对象，就用配置字段值去抹）
        logger.warning(
            f"凭据加载失败，按匿名访问：{_redact(f'{type(exc).__name__}: {exc}', cfg.bili_credential)}"
        )
        return None


async def _recognized_link(url: str, timeout: float) -> str:
    """先确认这串链接里能认出 BV/av 号，认不出就抛 BiliMaterialError——别把废链接往下递。

    能直接认出号的原样返回（零网络开销）；认不出才当短链还原一次，
    还原函数遇到非短链也是原样返回、不发请求。过了这道检查的链接由调用方直接交给
    build_material_markdown，所以一条短链只会被还原一次。

    Args:
        url: 用户给的原话（链接夹在分享文案里也没关系）。
        timeout: 还原短链愿意等多久，秒（取 cfg.material.request_timeout_seconds）。

    Returns:
        可以直接拿去抓取的链接（短链则是它跳转后的终态 URL）。

    Raises:
        BiliMaterialError: 原串和还原结果都认不出视频号。
    """
    text = str(url or "").strip()
    try:
        extract_bvid(text)
    except BiliMaterialError:
        resolved = await resolve_b23_short(text, timeout=timeout)
        extract_bvid(resolved)
        return resolved
    return text


class BiliVideoMaterialTool(BaseTool):
    """读 B 站视频素材给 LLM：字幕（或语音转写）、弹幕、热门评论，一份 Markdown 端上桌。
    """

    name: str = "bili_video_material"
    description: str = (
        "当聊天出现 B 站视频链接（BV号/bilibili.com/video/b23.tv）或有人问某个视频讲了什么、"
        "值不值得看时调用；读取字幕（无字幕时可能为语音转写）/弹幕/热门评论后结合自己的性格"
        "与聊天上下文给出具体总结，不要只凭标题猜测。内容依据以字幕或语音转写为准；"
        "弹幕与热门评论只是观众反应，不要当视频内容总结。返回 Markdown 素材"
        "（超过配置的字符上限会截断），读取需要数秒以上。"
    )

    async def execute(
        self,
        url: Annotated[str, "B站视频链接/BV号/av号/b23.tv短链"],
        include_danmaku: Annotated[bool, "是否含弹幕，默认true"] = True,
        include_comments: Annotated[bool, "是否含热门评论，默认true"] = True,
        include_timestamps: Annotated[bool, "保留时间戳，默认false"] = False,
    ) -> tuple[bool, str]:
        """去 B 站取这个视频的素材，整理成一份 Markdown 交回去。

        字幕优先；没有字幕时按 [asr] 配置决定要不要下载音频做语音转写；
        弹幕与热门评论按参数取舍，条数与总长上限都听配置的。

        Args:
            url: 视频链接 / BV 号 / av 号 / b23.tv 短链（夹在分享文案里也可以）。
            include_danmaku: 要不要弹幕段（条数取配置 danmaku_limit）。
            include_comments: 要不要热门评论段（条数取配置 comment_limit）。
            include_timestamps: 要不要给字幕/弹幕加 [MM:SS] 时间戳；生效值 = 本参数 OR
                配置 include_timestamps（配置给偏好，显式 True 覆盖）。

        Returns:
            (成功与否, 文本)：成功时是素材 Markdown；失败时 (False, 给 LLM 看的说明)。
        """
        cfg = cast(BiliSharerConfig, self.plugin.config)
        # 开关就在这儿判：组件始终注册，关着时不消失、只是不干活。
        # 关闭时直接回那句固定提示并记一条日志，其余动作一个都不做（凭据和网络都不碰）。
        if not bool(cfg.chat_tools.enabled):
            logger.warning(f"bili_video_material 被调用但{GATE_OFF_TEXT}")
            return False, GATE_OFF_TEXT
        material_cfg = cfg.material
        raw_url = str(url or "").strip()

        # 凭据先拿到手，是给后面的脱敏当参照物：之后所有回显的链接与异常文本
        # 都要过 material._redact 抹一遍，防把 Cookie 值抖进日志（它只读本地配置/文件，不联网）。
        credential = _load_credential_quiet(cfg)

        try:
            link = await _recognized_link(raw_url, float(material_cfg.request_timeout_seconds))
        except BiliMaterialError:
            safe_url = _redact(raw_url, credential)
            logger.warning(f"无法从链接识别 BV 号：{safe_url!r}")
            return False, (
                f"无法从链接识别 BV 号：{safe_url or '（空链接）'}。"
                "可用形式：BV 号、av 号、bilibili.com/video 链接、b23.tv 短链；其他形式无法识别。"
            )

        # [asr] 段用 getattr 防御着取：Bot 重启生效之前，旧配置对象可能压根没有这个属性。
        # 取不到就按 None 处理＝不做语音转写，工具本身不受半点影响。
        asr_cfg = getattr(cfg, "asr", None)

        # 先把三个布尔开关落到能读懂名字的局部变量上，再逐个显式传进去：
        # build_material_markdown 那八个 keyword 参数全部点名，不搞「默认值顺延」这种默契事故。
        want_danmaku = bool(include_danmaku)
        want_comments = bool(include_comments)
        want_timestamps = bool(include_timestamps) or bool(material_cfg.include_timestamps)

        try:
            markdown = await build_material_markdown(
                link,
                include_danmaku=want_danmaku,
                include_comments=want_comments,
                include_timestamps=want_timestamps,
                danmaku_limit=int(material_cfg.danmaku_limit),
                comment_limit=int(material_cfg.comment_limit),
                max_chars=int(material_cfg.max_output_chars),
                credential=credential,
                asr_cfg=asr_cfg,
            )
        except Exception as exc:  # noqa: BLE001
            # 上游库的异常文本可能把请求头里的 Cookie 原样吐出来，所以回给 LLM 与写日志都先脱敏。
            safe_exc = _redact(exc, credential)
            logger.warning(f"素材获取失败：{type(exc).__name__}: {safe_exc}")
            return False, f"素材获取失败：{safe_exc}"
        return True, markdown
