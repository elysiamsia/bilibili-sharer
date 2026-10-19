"""这是一个专门「领 LLM 模型」的小房间。

过滤和评论两条链路要用同一套大语言模型，与其各写一遍取模型的代码，
不如在这里一次备好——谁要用，谁来领就好，省得两边抄来抄去出偏差。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.llm_api import get_model_set_by_name, get_model_set_by_task
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("bili_sharer_llm")


def _resolve_model_set(name: str, temperature: float, max_tokens: int) -> tuple[Any, str]:
    """先按「任务」取模型，取不到再按「模型名」取；返回 (model_set, 走的是哪条路)。

    为什么先认任务呢？因为 bot 的 model.toml 里有一组「任务 → 模型」的映射
    （replyer / utils / voice 这些），任务名几乎每家都有，而具体模型名各人不同。
    所以默认填 utils 时，新装的人不用改任何东西也能跑起来。

    任务取回来后，会把插件自己想要的温度 / 输出上限盖上去——
    任务配置里的那套是给通用场景的，不一定合我们的口味呀。
    """
    try:
        task_set = get_model_set_by_task(name)
        if task_set:
            for entry in task_set:
                if isinstance(entry, dict):
                    entry["temperature"] = temperature
                    entry["max_tokens"] = max_tokens
            return task_set, "任务"
    except Exception:  # noqa: BLE001
        # 不是任务名，或者这家 bot 没配这个任务——安静地换下一条路就好
        pass
    return (
        get_model_set_by_name(name, temperature=temperature, max_tokens=max_tokens),
        "模型名",
    )


def load_model_set(cfg: Any, *, label: str = "LLM", consequence: str = "") -> Any | None:
    """按插件的配置段把 model_set 领出来，顺手将插件级 extra_params 合并进每个 entry。

    Args:
        cfg: 配置段（filter 或 builder 段），要有 model_name / temperature /
            max_tokens，extra_params 可选。
        label: 日志前缀（比如「过滤」「评论」），这样一读日志就知道是谁的事。
        consequence: 加载失败会连带的后果（比如「已跳过 LLM 过滤」），写进 error 日志。

    Returns:
        model_set（list[ModelEntry]）；cfg 是 None 或加载失败时返回 None。
        接下来怎么办（停用过滤、回模板）由调用方自己拿主意——
        本函数只管把模型取回来，不管替人做决定。
    """
    if cfg is None:
        return None
    try:
        model_set, how = _resolve_model_set(
            str(getattr(cfg, "model_name", "") or "").strip(),
            float(getattr(cfg, "temperature", 0.7)),
            int(getattr(cfg, "max_tokens", 800)),
        )
        logger.info(f"{label}模型按{how}解析：{getattr(cfg, 'model_name', '?')}")
        # 顺路把插件自己带的 extra_params 混进去（比如关掉思维模式的小开关）：
        # 核心只递给我们全局参数，插件级的加餐得自己端上桌。
        extra = getattr(cfg, "extra_params", None)
        if isinstance(extra, dict) and extra:
            for entry in model_set:
                merged = dict(entry.get("extra_params") or {})
                merged.update(extra)
                entry["extra_params"] = merged
            logger.info(f"{label}模型附加参数: {list(extra)}")
        return model_set
    except Exception as e:
        suffix = f"，{consequence}" if consequence else ""
        logger.error(f"{label}模型 '{getattr(cfg, 'model_name', '?')}' 加载失败{suffix}: {e}")
        return None
