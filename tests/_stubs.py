# -*- coding: utf-8 -*-
"""把 Bot 框架里插件真正用到的那几个符号换成轻量替身。

只做两件事：让 `services/*.py` 能在没有 Bot 的环境里被 import（CI 上跑得动），
以及把「会碰网络 / 磁盘 / 框架状态」的入口换成可控的假货。测试只断言本插件的
逻辑，不碰宿主行为——符合「测试必须隔离本机环境」的约定。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]


class _DummyLogger:
    """丢掉所有日志的假 logger：INFO/WARNING 不刷屏，DEBUG 也不用管。"""

    def debug(self, *args: Any, **kwargs: Any) -> None: ...
    def info(self, *args: Any, **kwargs: Any) -> None: ...
    def warning(self, *args: Any, **kwargs: Any) -> None: ...
    def error(self, *args: Any, **kwargs: Any) -> None: ...


def install_framework_stubs() -> None:
    """把假模块塞进 sys.modules；重复调用是安全的（幂等覆盖）。"""

    def _module(name: str) -> types.ModuleType:
        module = sys.modules.get(name)
        if module is None:
            module = types.ModuleType(name)
            sys.modules[name] = module
        return module

    for package in (
        "src",
        "src.app",
        "src.app.plugin_system",
        "src.app.plugin_system.api",
        "src.kernel",
    ):
        _module(package)

    log_api = _module("src.app.plugin_system.api.log_api")
    log_api.get_logger = lambda name="test": _DummyLogger()

    if "src.app.plugin_system.api.llm_api" not in sys.modules:
        llm_api = _module("src.app.plugin_system.api.llm_api")
        llm_api.create_llm_request = lambda *a, **k: None
        llm_api.get_model_set_by_name = lambda *a, **k: None
        llm_api.get_model_set_by_task = lambda *a, **k: None

    if "src.app.plugin_system.api.storage_api" not in sys.modules:
        storage_api = _module("src.app.plugin_system.api.storage_api")

        async def _load_json(store: str, key: str) -> Any:
            return None

        async def _save_json(store: str, key: str, data: Any) -> None:
            return None

        storage_api.load_json = _load_json
        storage_api.save_json = _save_json

    if "src.kernel.llm" not in sys.modules:
        kernel_llm = _module("src.kernel.llm")
        kernel_llm.LLMPayload = type("LLMPayload", (), {"__init__": lambda self, *a, **k: None})
        kernel_llm.ROLE = types.SimpleNamespace(USER="user", SYSTEM="system", ASSISTANT="assistant")
        kernel_llm.Text = lambda text="": text


def install_storage(spy: dict[str, Any]) -> None:
    """替换 storage_api 的读写实现：内存里放一份，顺便记录调用次数给断言用。"""
    storage_api = sys.modules["src.app.plugin_system.api.storage_api"]
    spy.setdefault("saves", [])

    async def _load_json(store: str, key: str) -> Any:
        return spy.get("data", {}).get(key)

    async def _save_json(store: str, key: str, data: Any) -> None:
        spy.setdefault("data", {})[key] = data
        spy["saves"].append(key)

    storage_api.load_json = _load_json
    storage_api.save_json = _save_json


def import_service(relative: str) -> Any:
    """按 `services.xxx` 的方式 import 插件里的模块（仓库根加入 sys.path）。"""
    install_framework_stubs()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return __import__(f"services.{relative}", fromlist=["*"])


__all__ = ["ROOT", "install_framework_stubs", "install_storage", "import_service", "Callable"]
