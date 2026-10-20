# -*- coding: utf-8 -*-
"""静态契约自检：不需要 Bot 框架、不联网，纯标准库。

为什么要有这一层？v1.2.2 发布件的 `plugin.py` 尾部被截断，插件照样「加载成功」，
但调度器从未启动——功能整块失效，却没有任何测试报红。这里把这类「结构性问题」
变成可断言的契约：

1. `manifest.json` 的 `include` 必须与 `get_components()` 返回的组件名完全一致；
2. 入口 `on_plugin_loaded` 必须真的把记账本 → 抓取 → 筛选 → 排版 → 调度器串起来
   并调用 `start()`，`on_plugin_unloaded` 必须调用 `stop()`；
3. 配置节与关键字段必须存在（避免编辑时整段丢失）；
4. 版本号在 manifest / README / 完整变更记录之间保持一致；
5. 所有源文件都能被 `ast.parse`（语法与截断的粗筛）。
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest.json"
PLUGIN_PY = ROOT / "plugin.py"
CONFIG_PY = ROOT / "config.py"
README = ROOT / "README.md"
CHANGELOG = ROOT / "docs" / "changelog-full.md"

EXPECTED_COMPONENTS = [
    "bili_search_videos",
    "bili_send_video",
    "bili_sharer_reactive",
    "bili_video_material",
]

EXPECTED_SECTIONS = [
    "plugin",
    "fetcher",
    "filter",
    "builder",
    "persona",
    "sender",
    "material",
    "asr",
    "bili_credential",
    "chat_tools",
    "opinion",
    "video_send",
    "reactive_filter",
]

EXPECTED_API_VERSION_KEYS = {
    "llm_api",
    "log_api",
    "send_api",
    "stream_api",
    "event_api",
    "storage_api",
    "plugin_api",
}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"找不到类 {name}")


def _method(tree: ast.Module, cls: str, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in _class_node(tree, cls).body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"类 {cls} 里找不到方法 {name}")


def _field_default(value: ast.AST) -> str | None:
    """取出 `Field(default=...)` 里那个 default 的源码文本；不是 Field 调用就返回 None。"""
    if not isinstance(value, ast.Call):
        return None
    func = value.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    if name != "Field":
        return None
    for keyword in value.keywords:
        if keyword.arg == "default":
            return ast.unparse(keyword.value)
    return "MISSING"


def _call_names(node: ast.AST) -> set[str]:
    """收集一段代码里出现的所有被调用函数/类的名字（含属性调用 obj.method）。"""
    names: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Call):
            func = item.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


class ManifestContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_include_matches_get_components(self) -> None:
        """include 与 get_components 必须一一对应（框架与市场都拿这个做一致性校验）。"""
        declared = sorted(entry["component_name"] for entry in self.manifest["include"])
        self.assertEqual(declared, EXPECTED_COMPONENTS, "manifest.include 组件清单变了")

        tree = _parse(PLUGIN_PY)
        returned: list[str] = []
        for node in ast.walk(_method(tree, "BiliSharerPlugin", "get_components")):
            if not isinstance(node, ast.Return) or not isinstance(node.value, ast.List):
                continue
            for element in node.value.elts:
                if isinstance(element, ast.Name):
                    returned.append(element.id)
        self.assertTrue(returned, "get_components 的返回语句没被解析出来")

        # 工具类的 name 是本文件里的字符串常量；被动处理器在 components/reactive_handler.py。
        names = {
            "BiliVideoMaterialTool": "bili_video_material",
            "BiliSearchVideosTool": "bili_search_videos",
            "BiliSendVideoTool": "bili_send_video",
            "ReactiveHandler": "bili_sharer_reactive",
        }
        resolved = sorted(names.get(item, item) for item in returned)
        self.assertEqual(resolved, declared, "get_components 与 manifest.include 不一致")

    def test_api_version_keys(self) -> None:
        declared = set(self.manifest.get("api_version", {}))
        self.assertEqual(declared, EXPECTED_API_VERSION_KEYS)

    def test_required_manifest_fields(self) -> None:
        self.assertEqual(self.manifest.get("name"), "bilibili_sharer")
        self.assertEqual(self.manifest.get("entry_point"), "plugin.py")
        self.assertTrue(self.manifest.get("python_dependencies"), "python_dependencies 不能为空")


class PluginWiringTest(unittest.TestCase):
    """入口接线契约：调度器必须真的被创建并启动，否则定时推送静默失效。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = _parse(PLUGIN_PY)

    def test_loaded_creates_services_and_starts_scheduler(self) -> None:
        calls = _call_names(_method(self.tree, "BiliSharerPlugin", "on_plugin_loaded"))
        for required in (
            "cleanup_orphan_cache",
            "StateManager",
            "load",
            "BiliFetcher",
            "VideoFilter",
            "MessageBuilder",
            "SharerScheduler",
            "start",
        ):
            self.assertIn(required, calls, f"on_plugin_loaded 少了 {required}() 这一步")

    def test_scheduler_start_is_awaited(self) -> None:
        body = _method(self.tree, "BiliSharerPlugin", "on_plugin_loaded")
        awaited_ids = {id(node.value) for node in ast.walk(body) if isinstance(node, ast.Await)}
        awaited = {
            node.func.attr
            for node in ast.walk(body)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and id(node) in awaited_ids
        }
        self.assertIn("start", awaited, "调度器 start() 必须被 await")

    def test_unloaded_stops_scheduler(self) -> None:
        calls = _call_names(_method(self.tree, "BiliSharerPlugin", "on_plugin_unloaded"))
        self.assertIn("stop", calls, "卸载时必须停掉调度器")

    def test_scheduler_start_is_not_called_twice_guarded(self) -> None:
        """scheduler.start() 自带重入保护，但入口也不该重复创建调度器。"""
        body = _method(self.tree, "BiliSharerPlugin", "on_plugin_loaded")
        created = [node for node in ast.walk(body)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "SharerScheduler"]
        self.assertEqual(len(created), 1, "on_plugin_loaded 应当只创建一个调度器")


class ConfigContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = _parse(CONFIG_PY)
        cls.source = CONFIG_PY.read_text(encoding="utf-8")

    def test_all_sections_present(self) -> None:
        for section in EXPECTED_SECTIONS:
            self.assertIn(
                f'@config_section("{section}"',
                self.source,
                f"配置节 [{section}] 不见了",
            )

    def test_config_fields_wired(self) -> None:
        fields = {
            node.target.id
            for node in _class_node(self.tree, "BiliSharerConfig").body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        for section in EXPECTED_SECTIONS:
            self.assertIn(section, fields, f"BiliSharerConfig 少了字段 {section}")

    def test_key_defaults(self) -> None:
        """几个对行为影响最大的默认值：改它们等于改开箱体验，必须显式确认。"""
        expect = {
            "chat_tools": ("enabled", "True"),
            "opinion": ("push_mode", "'simple'"),
            "video_send": ("push_enabled", "True"),
            "video_send#reactive": ("reactive_enabled", "True"),
            "asr": ("enabled", "False"),
            "reactive_filter": ("debounce_seconds", "300"),
        }
        for label, (field, default) in expect.items():
            section = label.split("#")[0]
            node = None
            for item in ast.walk(self.tree):
                if isinstance(item, ast.ClassDef) and item.name == f"{section.title().replace('_', '')}Section":
                    node = item
                    break
            self.assertIsNotNone(node, f"找不到 [{section}] 的配置类")
            found = None
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.target.id == field:
                    found = _field_default(item.value)
                    break
            self.assertIsNotNone(found, f"[{section}].{field} 不见了（或没写成 Field(default=...)）")
            self.assertEqual(found, default, f"[{section}].{field} 的默认值是 {found}，与预期 {default} 不符")


class VersionConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.version = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]
        cls.readme = README.read_text(encoding="utf-8")
        cls.changelog = CHANGELOG.read_text(encoding="utf-8")

    def test_readme_install_mentions_current_version(self) -> None:
        self.assertIn(f"bilibili_sharer-{self.version}.mfp", self.readme)

    def test_readme_changelog_top_matches(self) -> None:
        match = re.search(r"^### v([0-9.]+)（", self.readme, re.MULTILINE)
        self.assertIsNotNone(match, "README 里没有版本小节")
        self.assertEqual(match.group(1), self.version)

    def test_full_changelog_top_matches(self) -> None:
        match = re.search(r"^### v([0-9.]+)（", self.changelog, re.MULTILINE)
        self.assertIsNotNone(match, "docs/changelog-full.md 里没有版本小节")
        self.assertEqual(match.group(1), self.version)


class SourceSyntaxTest(unittest.TestCase):
    def test_all_python_files_parse(self) -> None:
        files = sorted(ROOT.rglob("*.py"))
        # 只关心插件本体（tests/ 自己也会被解析，没问题）
        self.assertGreater(len(files), 15)
        for path in files:
            with self.subTest(path=str(path.relative_to(ROOT))):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_plugin_py_has_no_truncated_tail(self) -> None:
        """截断的典型症状：文件以半截表达式结束、或最后一个顶层定义不完整。"""
        source = PLUGIN_PY.read_text(encoding="utf-8")
        self.assertTrue(source.endswith("\n"), "plugin.py 末尾缺少换行（疑似被截断）")
        self.assertIn("on_plugin_unloaded", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
