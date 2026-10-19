"""语音转写（ASR）服务——这是插件的「顺风耳」：把视频里的音频送去听写，换回一份文字稿。
走 OpenAI 兼容的 /audio/transcriptions 端点，像把录音递进听写亭一样自然。

对外符号：
- AsrSettings：转写所需参数的归一化快照（base_url / api_key / model / timeout /
  language / max_minute / enabled），把零散配置收拢成一个整洁的小本子；
- load_asr_settings(cfg)：从 [asr] 配置解析出 AsrSettings——未启用 / 非 api 模式 /
  缺 base_url 或 api_key，一律返回 None（日志轻轻提醒一句，不抛，也绝不联网试探）；
- transcribe_audio(path, settings)：用 httpx multipart 把音频 POST 到
  {base_url}/audio/transcriptions（字段 file/model/language），返回 (是否成功, 文本)；
  任何失败都是 (False, "")——不抛，日志与文本里绝不会出现 api_key。

凭据从哪来：配了 provider_name 时，读 bot 的 config/model.toml（tomllib），按 name
去 api_providers 里匹配，借它的 base_url 和 api_key；显式写了 api_base / api_key
（非空）则优先覆盖。model.toml 的路径有三级找法：调用方显式 model_toml 参数
（测试递来的临时副本）> [asr].model_toml_path 配置覆盖（支持 %VAR% 展开）>
按本文件位置推算：services/asr.py -> parents[0]=services -> [1]=bilibili_sharer ->
[2]=plugins -> [3]=neo-mofox（仓库根）→ <仓库根>/config/model.toml（配置留空就走这条）；
文件缺失 / 解析失败 / 查无此名的日志都带路径和出处，想查账一眼就能定位。

安全口径：api_key 只进 Authorization 请求头；所有日志/异常文本都先过 _redact_key 擦一遍；
返回文本受 ASR_TEXT_MAX_CHARS 上限截断（整篇素材之后还要再受 material.max_output_chars 约束）。

MIT 归属：本模块为自研实现，零 BiliLearn / parser 代码复制；端点形态参考
硅基流动公开 API 文档（OpenAI 兼容 /audio/transcriptions）。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("bili_sharer_asr")

# model.toml（bot 仓库根下的 provider 配置）默认位置，由本文件位置推算而来；
# [asr].model_toml_path 可以覆盖它——插件被搬到不寻常的深度时，就不会找错地方啦。
DEFAULT_MODEL_TOML = Path(__file__).resolve().parents[3] / "config" / "model.toml"
# 转写文本的硬上限（字符）：音频再能唠，也不能让它用巨量文本撑坏素材——超出就截断并附说明行。
ASR_TEXT_MAX_CHARS = 12000
# 一期只支持一种转写方式：老老实实调用 API。
_MODE_API = "api"


@dataclass
class AsrSettings:
    """[asr] 配置 + provider 解析之后的归一化快照：
    「去哪家敲门、以什么名义、等多久」全记在这一个小本子上，用时无需再翻配置。
    """

    base_url: str = ""
    # repr=False：哪怕有人随手把 f"{settings}" / repr 打进日志，key 也不会露头；
    # 真要比对，就请显式访问 settings.api_key。
    api_key: str = field(default="", repr=False)
    model: str = "FunAudioLLM/SenseVoiceSmall"
    timeout: float = 120.0
    language: str = "zh"
    max_minute: int = 10
    # 端点路径：默认就是 OpenAI 兼容的 /audio/transcriptions；
    # 换服务商时，只需要改这一项就好。
    endpoint_path: str = "/audio/transcriptions"
    enabled: bool = False


def _asr_field(section: Any, name: str, default: Any) -> Any:
    """兼容地取一个值：配置是 pydantic 对象也好、dict 也好，都能读到。
    只有值真是 None 才回退默认——空串是正经配置值，不能偷偷替它做主。
    """
    if section is None:
        return default
    value = section.get(name, default) if isinstance(section, dict) else getattr(section, name, default)
    return default if value is None else value


def _asr_section(cfg: Any) -> Any:
    """取 [asr] 段：递来插件全量配置（带 asr 属性）可以，只递这一段本身也行——
    和 bili_downloader 取 [video_send] 段是同一套宽容口径。
    """
    if cfg is None:
        return None
    section = cfg.get("asr") if isinstance(cfg, dict) else getattr(cfg, "asr", None)
    return section if section is not None else cfg


def _redact_key(text: Any, api_key: str) -> str:
    """把文本里可能混进来的 api_key 悄悄擦掉：只为日志与降级文案服务，绝不外发。"""
    out = str(text)
    if api_key and len(api_key) >= 4:
        out = out.replace(api_key, "***")
    return out


def _resolve_model_toml(section: Any, explicit: Path | str | None) -> tuple[Path, str]:
    """分三级去找 model.toml 的路径，返回 (路径, 一句来源说明)。

    优先级：调用方显式 model_toml 参数（测试递来的）> [asr].model_toml_path 配置
    （会展开 %VAR%、去掉外层引号）> 按本文件位置推算的仓库根路径。来源说明会跟进
    「文件缺失」日志，让人一眼看出用的是哪条路；配置留空 = 按推算，与一贯默认行为一致。
    """
    if explicit:
        return Path(explicit), "来源：调用方显式传入（测试参数）"
    configured = str(_asr_field(section, "model_toml_path", "") or "").strip()
    if configured:
        expanded = os.path.expandvars(configured).strip().strip('"').strip("'")
        return Path(expanded), "来源：[asr].model_toml_path 配置覆盖"
    return DEFAULT_MODEL_TOML, "来源：按 services/asr.py 位置推算（可用 [asr].model_toml_path 覆盖）"


def _lookup_provider(
    provider_name: str, model_toml: Path | str | None, source: str = ""
) -> tuple[str, str]:
    """读 model.toml 的 api_providers，按 name 找 (base_url, api_key)；任何失败都只回 ("", "")。

    只记 provider 名和字段长度，**绝不记 key 的值**；文件缺失 / TOML 语法错误 /
    结构异常 / 查无此名，统统只 log warning 不抛。source 注明路径出处，
    缺文件时日志能把话说清楚。
    """
    path = Path(model_toml) if model_toml else DEFAULT_MODEL_TOML
    where = f"（{source}）" if source else ""
    try:
        if not path.is_file():
            logger.warning(f"model.toml 不存在，无法复用 provider：{path}{where}")
            return ("", "")
        with open(path, "rb") as fp:
            data = tomllib.load(fp)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"读取 model.toml 失败（忽略 provider 复用）：{path}{where} -> "
            f"{type(exc).__name__}: {exc}"
        )
        return ("", "")

    providers = data.get("api_providers") if isinstance(data, dict) else None
    if not isinstance(providers, list):
        logger.warning("model.toml 没有 api_providers 列表，无法复用 provider")
        return ("", "")
    for item in providers:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip() != provider_name:
            continue
        base = str(item.get("base_url") or "").strip()
        key = str(item.get("api_key") or "").strip()
        # 日志要细心：只报 key 的长度，绝不报它的值。
        logger.info(
            f"provider 复用成功：{provider_name} base_url={base or '(空)'} api_key 长度={len(key)}"
        )
        return (base, key)
    logger.warning(f"model.toml 的 api_providers 中没有名为 {provider_name!r} 的 provider")
    return ("", "")


def _lookup_voice_task() -> tuple[str, str, str]:
    """顺着 bot 的 model_tasks.voice 借 (base_url, api_key, model_identifier)；失败返回 ("","","")。

    主人的心思是：语音转写别再另配一套凭据了，直接用 bot 已经在用的「voice」任务
    模型（例如 XingChenAGI/XingChenASR-V3.2-Ultra，走 SiliconFlow 的 OpenAI 兼容端点）。
    这里只读不写、只记 provider/模型名与 key 的长度，绝不打印 key 值；任何异常都安静降级不抛。
    """
    try:
        from src.app.plugin_system.api.llm_api import get_model_set_by_task

        model_set = get_model_set_by_task("voice")
        entry = model_set[0] if model_set else None
        if not isinstance(entry, dict):
            logger.warning("[asr] model_tasks.voice 没有可用条目，无法复用语音模型")
            return ("", "", "")
        base = str(entry.get("base_url") or "").strip()
        key = str(entry.get("api_key") or "").strip()
        model = str(entry.get("model_identifier") or "").strip()
        logger.info(
            "[asr] 复用 model_tasks.voice：provider={} model={} api_key 长度={}".format(
                entry.get("api_provider"), model or "(空)", len(key)
            )
        )
        return (base, key, model)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[asr] 复用 model_tasks.voice 失败（降级为 provider/显式配置）：{type(exc).__name__}: {exc}")
        return ("", "", "")


def load_asr_settings(
    cfg: Any, *, model_toml: Path | str | None = None
) -> AsrSettings | None:
    """把 [asr] 配置解析成 AsrSettings；不可用时返回 None（log warning，绝不抛）。

    判定顺序：
    1. enabled 为假 -> None（正常的关闭态，连一声都不吭）；
    2. mode != "api" -> 一期只实现了 api，warning + None；
    3. provider_name 非空且缺 base_url / api_key -> 去读 model.toml 复用（显式
       api_base / api_key 非空时优先覆盖）；
    4. 忙完一圈仍缺 base_url 或 api_key -> warning + None。

    Args:
        cfg: 插件配置（会去取 cfg.asr）或直接递来 [asr] 段本身；同名字段的 dict 也认。
        model_toml: 可选的 model.toml 路径覆盖（测试递临时副本）；None 时先看
            [asr].model_toml_path 配置，仍为空才用仓库根推算值。

    Returns:
        AsrSettings 或 None（None = 本功能视为未启用，调用方走自己那条既有降级路）。
    """
    section = _asr_section(cfg)
    if section is None or not bool(_asr_field(section, "enabled", False)):
        return None

    mode = str(_asr_field(section, "mode", _MODE_API) or "").strip().lower()
    if mode != _MODE_API:
        logger.warning(f"[asr] mode={mode!r} 一期未实现（仅支持 'api'），按未启用处理")
        return None

    base = str(_asr_field(section, "api_base", "") or "").strip()
    key = str(_asr_field(section, "api_key", "") or "").strip()
    provider = str(_asr_field(section, "provider_name", "") or "").strip()
    source = str(_asr_field(section, "source", "voice") or "voice").strip().lower()
    model_from_task = ""

    # 听 source 的话决定先敲哪扇门（base / key 还没凑齐才出门找）：
    #   source="voice"    -> 先复用 model_tasks.voice，取不到再退回 provider_name（默认）
    #   source="provider" -> 先复用 provider_name，取不到再退回 voice 任务
    def _try_voice() -> None:
        nonlocal base, key, model_from_task
        v_base, v_key, v_model = _lookup_voice_task()
        base = base or v_base
        key = key or v_key
        model_from_task = model_from_task or v_model

    def _try_provider() -> None:
        nonlocal base, key
        if not provider:
            return
        # 找 model.toml：显式参数 > 配置覆盖 > 按位置推算，一路问过去。
        toml_path, toml_source = _resolve_model_toml(section, model_toml)
        p_base, p_key = _lookup_provider(provider, toml_path, toml_source)
        base = base or p_base
        key = key or p_key

    if not base or not key:
        if source == "provider":
            _try_provider()
            if not base or not key:
                _try_voice()
        else:
            _try_voice()
            if not base or not key:
                _try_provider()

    if not base or not key:
        missing = []
        if not base:
            missing.append("api_base（provider 复用与显式覆盖都没拿到）")
        if not key:
            missing.append("api_key（同上）")
        logger.warning(f"[asr] 已启用但缺少 {' + '.join(missing)}，语音转写按未启用处理")
        return None

    try:
        timeout = float(_asr_field(section, "timeout_seconds", 120))
    except (TypeError, ValueError):
        timeout = 120.0
    try:
        max_minute = int(_asr_field(section, "max_minute", 10))
    except (TypeError, ValueError):
        max_minute = 10

    configured_model = str(_asr_field(section, "model", "") or "").strip()
    endpoint_path = str(_asr_field(section, "endpoint_path", "") or "").strip() or "/audio/transcriptions"
    return AsrSettings(
        base_url=base,
        api_key=key,
        model=configured_model or model_from_task or "FunAudioLLM/SenseVoiceSmall",
        timeout=timeout,
        language=str(_asr_field(section, "language", "zh") or "").strip(),
        max_minute=max_minute,
        endpoint_path=endpoint_path,
        enabled=True,
    )


def _join_url(base_url: str, endpoint_path: str = "/audio/transcriptions") -> str:
    """拼出端点 URL：{base_url}{endpoint_path}——base 带尾斜杠、path 缺前导斜杠都不怕。

    换服务商时改 [asr].endpoint_path 这一项就够；默认本就是 OpenAI 兼容的
    /audio/transcriptions，不改也能安稳用。
    """
    base = str(base_url or "").rstrip("/")
    path = str(endpoint_path or "").strip() or "/audio/transcriptions"
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


async def transcribe_audio(path: Path, settings: AsrSettings) -> tuple[bool, str]:
    """把一个音频文件 POST 到 {base_url}/audio/transcriptions，请对面听完还回文字。

    Args:
        path: 本地音频路径（multipart 里填进 file 字段）。
        settings: load_asr_settings 辛苦整理出来的成果。

    Returns:
        (是否成功, 转写文本)；HTTP != 200 / JSON 异常 / 没有 text / 文本为空 /
        任何网络意外，一律 (False, "")——会 log（含脱敏后的响应片段）但**绝不抛**；
        成功的文本按 ASR_TEXT_MAX_CHARS 截断并附上一行说明。**日志与文本绝不含 api_key**。
    """
    audio_path = Path(path)
    if settings is None or not settings.enabled:
        return (False, "")

    url = _join_url(settings.base_url, getattr(settings, "endpoint_path", "/audio/transcriptions"))
    headers = {"Authorization": f"Bearer {settings.api_key}"}
    data: dict[str, str] = {"model": settings.model}
    if settings.language:
        data["language"] = settings.language
    try:
        # 文件预检和 stat 也一起护进 try：哪怕撞上并发删除、Windows 文件占用这类
        # OSError，也照样守「return (False, "") 绝不抛」的既有约定。
        if not audio_path.is_file() or audio_path.stat().st_size == 0:
            logger.warning(f"[asr] 音频文件不可用（不存在或为空），跳过转写：{audio_path}")
            return (False, "")
        size = audio_path.stat().st_size
        with open(audio_path, "rb") as fp:
            files = {"file": (audio_path.name, fp, "application/octet-stream")}
            async with httpx.AsyncClient(timeout=settings.timeout) as client:
                resp = await client.post(url, headers=headers, data=data, files=files)
        if resp.status_code != 200:
            snippet = _redact_key(resp.text[:200], settings.api_key)
            logger.warning(
                f"[asr] 转写失败：HTTP {resp.status_code}（{audio_path.name} {size} 字节，"
                f"model={settings.model}）响应片段：{snippet!r}"
            )
            return (False, "")
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.warning(f"[asr] 转写响应不是 JSON：{type(exc).__name__}: {exc}")
            return (False, "")
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            logger.warning(
                "[asr] 转写成功但文本为空："
                f"{_redact_key(str(payload)[:200], settings.api_key)!r}"
            )
            return (False, "")
        out = text.strip()
        if len(out) > ASR_TEXT_MAX_CHARS:
            # 和素材层 _truncate_markdown 一个口径：说明行也计入上限哦。
            note = "\n…（转写文本超长，已截断）"
            keep = max(ASR_TEXT_MAX_CHARS - len(note), 0)
            body = out[:keep].rstrip()
            out = f"{body}{note}" if body else note.strip()
        logger.info(f"[asr] 转写成功：{audio_path.name}（{size} 字节）→ {len(out)} 字符")
        return (True, out)
    except Exception as exc:  # noqa: BLE001
        # 网络/磁盘的所有意外：只 log 脱敏后的文本、绝不抛——调用方自有它的降级台阶。
        logger.warning(f"[asr] 转写请求失败：{_redact_key(f'{type(exc).__name__}: {exc}', settings.api_key)}")
        return (False, "")
