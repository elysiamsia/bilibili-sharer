"""人设组装：把「我是谁」这段自我介绍拼出来，好让 LLM 照着说话。

默认不重复造轮子——直接复用 bot 的全局人格（config/core.toml 的 [personality] 段）：
插件的 [persona] 里哪一项填了就用插件的，没填就沿用全局的，全空就交白卷（空串）。

纯本地拼装，不联网、不调模型；配置对象缺胳膊少腿也绝不抛异常。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("bili_sharer_persona")

# 人设的七块拼图：「配置里的字段名 → 写进文本时的中文标签」。
# 这里的先后顺序，就是最后组装出来那几行的先后顺序；名字与 bot 全局 [personality] 完全对齐。
_PERSONALITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("nickname", "昵称"),
    ("alias_names", "别名"),
    ("personality_core", "核心性格"),
    ("personality_side", "另一面"),
    ("identity", "身份"),
    ("background_story", "背景故事"),
    ("reply_style", "说话风格"),
)


def _to_text(value: Any) -> str:
    """把一个配置值整理成能读的人设文本。

    输入：任意配置值（字符串、列表、None 都可能）。做了什么：列表类用「、」串起来
    （顺手剔掉空项），其它情况转成字符串并去掉首尾空白。输出：干净文本，空值就是空串。
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "、".join(
            str(item).strip() for item in value if item is not None and str(item).strip()
        )
    return str(value).strip()


def _core_personality() -> Any:
    """去问框架本体：bot 自己的全局人格段在哪儿？拿不到就安静地返回 None。

    输入：无（走官方 config_api.get_core_config()）。做了什么：读 core 配置里的
    [personality] 段，读不到只记一条 DEBUG。输出：段对象，或 None（核心配置尚未
    初始化、没有这一段、API 不可用都算）——拿到 None 就降级成「只用插件自己的值」。
    """
    try:
        from src.app.plugin_system.api import config_api

        core = config_api.get_core_config()
        personality = getattr(core, "personality", None)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            f"[人设] 读不到 bot 全局人格（{type(exc).__name__}: {exc}），"
            f"本次只用插件 [persona] 自己的值"
        )
        return None
    if personality is None:
        logger.debug("[人设] 核心配置没有 [personality] 段，只用插件 [persona] 自己的值")
    return personality


def compose_persona(cfg_persona: Any) -> str:
    """组装这次要用的人设文本：全局人格打底，插件填过的字段逐项覆盖。

    输入：插件 [persona] 段对象。规则很简单：七个字段留空＝复用 bot 全局人格的
    同名项，填了＝只在这个插件里覆盖它；最后再追加 `persona`（额外补充说明，非空才有）。
    输出：像「昵称：… ／ 核心性格：…」那样的多行文本；全部为空就返回空串——
    兜底交给调用方——被动点评会用「一个普通的群友」顶上。
    """
    core = _core_personality()
    parts: list[str] = []
    overridden: list[str] = []
    for name, label in _PERSONALITY_FIELDS:
        text = _to_text(getattr(cfg_persona, name, None))
        if text:
            overridden.append(name)
        elif core is not None:
            text = _to_text(getattr(core, name, None))
        if text:
            parts.append(f"{label}：{text}")
    extra = _to_text(getattr(cfg_persona, "persona", None))
    if extra:
        parts.append(extra)
    composed = "\n".join(parts)
    if overridden:
        logger.debug(
            f"[人设] 插件覆盖字段：{'、'.join(overridden)}；"
            f"其余复用全局人格（补充说明{'已追加' if extra else '为空'}）"
        )
    return composed


__all__ = ["compose_persona"]
