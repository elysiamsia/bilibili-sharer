"""本机浏览器 / B 站桌面客户端的登录 Cookie 探测（只读，取完即走）。

为什么要有这个模块？很多人的 B 站登录态早就躺在自己的浏览器里了，却还要
去 BiliLearn 面板导出 cookie 文件——这在没有 BiliLearn 的机器上根本走不通。
所以我们自己去找：在本机的 Chromium 系浏览器（Edge / Chrome / Brave / Vivaldi）
与 B 站桌面客户端（Electron）里读出 `*.bilibili.com` 的 Cookie，再交给
`services/material.py::load_credential` 拼成 bilibili_api 的 Credential。

技术路线（都是本机实测过的）：
- Chromium 的 cookie 值用 DPAPI 保护：`Local State` 里的 `os_crypt.encrypted_key`
  是「DPAPI + 32 字节 AES 密钥」，解出来之后每条 cookie 是 `v10` + 12 字节 nonce +
  AES-GCM 密文 + 16 字节 tag（`v20` 是 Chrome 127+ 的 App-Bound 加密，解不了，跳过）；
- Firefox 的 `cookies.sqlite` 直接存明文（设了主密码才加密，这种情况我们跳过）；
- cookie 数据库正被浏览器占用时（实测 Edge 运行中会独占）读不到，这里如实报告
  「浏览器正在运行」，由上层提示用户关掉浏览器或改用扫码登录；
- **绝不打印 cookie 值**：日志里只有来源、名字和条数，值只进 Credential。

依赖是可选的：AES-GCM 走 `cryptography`（Bot venv 里通常都有），没有就退
`pycryptodome`，两者都没有时该来源直接跳过——不新增 python_dependencies。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("bili_sharer_cookies")

# 我们真正需要的 cookie 名（大小写按浏览器里的原样）→ 插件内部字段名
WANTED_COOKIES: dict[str, str] = {
    "SESSDATA": "sessdata",
    "bili_jct": "bili_jct",
    "buvid3": "buvid3",
    "DedeUserID": "dedeuserid",
    "ac_time_value": "ac_time_value",
}
# 判定「这个库里有 B 站登录态」的最低要求：SESSDATA 才是登录身份
REQUIRED_COOKIE = "SESSDATA"

# cookie 库被浏览器独占时用于给上层提示的错误类型
COOKIE_DB_IN_USE = "browser_running"

# 探测结果缓存：素材/弹幕/评论每条都会调一次 load_credential，不缓存就等于反复读浏览器库。
# 60 秒足够挡住连发请求，又不至于让「刚扫完码/刚登录」等太久才生效（扫码记录不缓存在这里）。
_SCAN_CACHE: dict[str, tuple[float, "CookieScanResult"]] = {}
_SCAN_CACHE_TTL = 60.0


def clear_cache() -> None:
    """清掉探测缓存（测试与「立刻重新探测」用）。"""
    _SCAN_CACHE.clear()


@dataclass
class CookieScanResult:
    """一次探测的结果：找到的字段、来源标签、以及没找到时的原因。"""

    values: dict[str, str] = field(default_factory=dict)
    source: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.values.get("sessdata"))


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_unprotect(blob: bytes) -> bytes:
    """用当前 Windows 用户的 DPAPI 解开一段密文（只在本机、只能由同一用户解）。"""
    if sys.platform != "win32":
        raise RuntimeError("DPAPI 只在 Windows 上可用")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(blob, len(blob))
    data_in = _DataBlob(len(blob), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    data_out = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(data_in), None, None, None, None, 0, ctypes.byref(data_out)
    ):
        raise OSError(f"DPAPI 解密失败（错误码 {kernel32.GetLastError()}）")
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(data_out.pbData)


def _aes_gcm_decrypt(key: bytes, nonce: bytes, payload: bytes) -> bytes:
    """AES-256-GCM 解密：优先 cryptography，其次 pycryptodome；都没有就抛 ImportError。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

        return AESGCM(key).decrypt(nonce, payload, None)
    except ImportError:
        pass
    try:
        from Crypto.Cipher import AES  # noqa: PLC0415

        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        plain, _tag = cipher.decrypt_and_verify(payload[:-16], payload[-16:])
        return plain
    except ImportError as exc:
        raise ImportError("本机缺少 AES-GCM 实现（cryptography / pycryptodome 都没有）") from exc


def decrypt_chromium_value(key: bytes, blob: bytes) -> str:
    """解一条 Chromium cookie 的 `encrypted_value`；v20（App-Bound）明确拒绝。"""
    raw = bytes(blob or b"")
    prefix = raw[:3]
    if prefix in (b"v10", b"v11"):
        if len(raw) < 3 + 12 + 16:
            raise ValueError("密文长度不足")
        plain = _aes_gcm_decrypt(key, raw[3:15], raw[15:])
        return plain.decode("utf-8", "replace")
    if prefix == b"v20":
        raise ValueError("v20（App-Bound 加密）暂不支持，请改用扫码登录或关闭浏览器后重试")
    return raw.decode("utf-8", "replace")


def _chromium_key(user_data_dir: Path) -> bytes:
    """从 `<User Data>/Local State` 取 AES 密钥（DPAPI 解 os_crypt.encrypted_key）。"""
    local_state = user_data_dir / "Local State"
    if not local_state.is_file():
        raise FileNotFoundError(f"缺少 Local State：{local_state}")
    data = json.loads(local_state.read_text(encoding="utf-8"))
    encrypted = base64.b64decode((data.get("os_crypt") or {}).get("encrypted_key") or "")
    if not encrypted.startswith(b"DPAPI"):
        raise ValueError("Local State 里的密钥不是 DPAPI 格式（可能是别的平台/主密码保护）")
    return _dpapi_unprotect(encrypted[5:])


def _profile_dirs(user_data_dir: Path) -> list[Path]:
    """列出用户数据目录下所有「有 Cookies 库」的 profile（Default / Profile 1 …）。

    Electron 应用（比如 B 站桌面客户端）没有 profile 子目录，Cookies 就在根上——
    这种把根目录本身当成一个 profile 返回。"""
    if not user_data_dir.is_dir():
        return []
    found: list[Path] = []
    if (user_data_dir / "Network" / "Cookies").is_file() or (user_data_dir / "Cookies").is_file():
        found.append(user_data_dir)
    for child in user_data_dir.iterdir():
        if not child.is_dir():
            continue
        if (child / "Network" / "Cookies").is_file() or (child / "Cookies").is_file():
            found.append(child)
    return found


def _cookie_db(profile: Path) -> Path | None:
    """新版 Chromium 在 `<profile>/Network/Cookies`，老版本在 `<profile>/Cookies`。"""
    for candidate in (profile / "Network" / "Cookies", profile / "Cookies"):
        if candidate.is_file():
            return candidate
    return None


def _read_chromium_db(db: Path, key: bytes) -> tuple[dict[str, str], str]:
    """读一个 Chromium cookie 库，返回 (需要的字段, 备注)。

    浏览器开着时文件被独占（实测 Edge），这里把「打不开」翻译成 COOKIE_DB_IN_USE，
    让上层能给出「关掉浏览器再试」的提示。
    """
    tmp = Path(tempfile.gettempdir()) / f"bili_sharer_cookies_{os.getpid()}_{time.time_ns()}.db"
    try:
        try:
            shutil.copyfile(db, tmp)
        except (PermissionError, OSError) as exc:
            if getattr(exc, "winerror", None) in (32, 33) or isinstance(exc, PermissionError):
                raise PermissionError(COOKIE_DB_IN_USE) from exc
            raise
        connection = sqlite3.connect(str(tmp), timeout=2)
        try:
            rows = connection.execute(
                "select host_key, name, value, encrypted_value from cookies "
                "where host_key like '%bilibili.com%'"
            ).fetchall()
        finally:
            connection.close()
    finally:
        tmp.unlink(missing_ok=True)

    values: dict[str, str] = {}
    notes: list[str] = []
    for _host, name, plain, encrypted in rows:
        field = WANTED_COOKIES.get(name)
        if not field or field in values:
            continue
        try:
            value = decrypt_chromium_value(key, encrypted) if encrypted else str(plain or "")
        except Exception as exc:  # noqa: BLE001
            if name == REQUIRED_COOKIE:
                notes.append(f"{name} 解密失败：{type(exc).__name__}: {exc}")
            continue
        if value:
            values[field] = value
    return values, "；".join(notes)


def _read_firefox_db(db: Path) -> tuple[dict[str, str], str]:
    """读 Firefox 的 cookies.sqlite（值通常明文；设了主密码的读出来是密文，跳过）。"""
    tmp = Path(tempfile.gettempdir()) / f"bili_sharer_ff_{os.getpid()}_{time.time_ns()}.db"
    try:
        shutil.copyfile(db, tmp)
        connection = sqlite3.connect(str(tmp), timeout=2)
        try:
            rows = connection.execute(
                "select name, value from moz_cookies where host like '%bilibili.com%'"
            ).fetchall()
        finally:
            connection.close()
    finally:
        tmp.unlink(missing_ok=True)

    values: dict[str, str] = {}
    for name, value in rows:
        field = WANTED_COOKIES.get(name)
        if field and value and field not in values:
            values[field] = str(value)
    return values, ""


def _chromium_candidates(source: str) -> Iterable[tuple[str, Path]]:
    """列出要尝试的 Chromium 系浏览器（按 source 过滤：auto 全试，指定只试那一个）。"""
    local = Path(os.environ.get("LOCALAPPDATA") or "")
    roaming = Path(os.environ.get("APPDATA") or "")
    all_sources: list[tuple[str, Path]] = [
        ("Edge", local / "Microsoft" / "Edge" / "User Data"),
        ("Chrome", local / "Google" / "Chrome" / "User Data"),
        ("Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data"),
        ("Vivaldi", local / "Vivaldi" / "User Data"),
        ("Chromium", local / "Chromium" / "User Data"),
        # B 站桌面客户端也是个 Electron（有自己的 Cookies 库），顺手一试
        ("B站客户端", roaming / "bilibili"),
    ]
    wanted = (source or "auto").strip().lower()
    for label, path in all_sources:
        if not path.is_dir():
            continue
        if wanted in ("", "auto", "all", "chromium"):
            yield label, path
        elif wanted == label.lower() or wanted in label.lower():
            yield label, path


def _firefox_candidates() -> Iterable[Path]:
    roaming = Path(os.environ.get("APPDATA") or "")
    base = roaming / "Mozilla" / "Firefox" / "Profiles"
    if not base.is_dir():
        return []
    return [p / "cookies.sqlite" for p in base.iterdir() if (p / "cookies.sqlite").is_file()]


def scan_local_cookies(source: str = "auto") -> CookieScanResult:
    """扫本机登录态：返回带 sessdata 的第一个来源；都不行时给出可读原因。

    Args:
        source: `auto`（默认，全部来源）/ `edge` / `chrome` / `firefox` / `none`。

    Returns:
        CookieScanResult：`ok=True` 表示拿到了包含 SESSDATA 的凭据。
    """
    wanted = (source or "auto").strip().lower()
    if wanted in ("none", "off", "disabled"):
        return CookieScanResult(reason="auto_detect 已关闭（browser_cookie_source=none）")

    cached = _SCAN_CACHE.get(wanted)
    if cached is not None and time.time() - cached[0] < _SCAN_CACHE_TTL:
        return cached[1]

    result = _scan_now(wanted)
    _SCAN_CACHE[wanted] = (time.time(), result)
    return result


def _scan_now(wanted: str) -> CookieScanResult:
    """真正去翻本机各个 Cookie 库（调用方负责缓存）。"""
    reasons: list[str] = []
    wants_firefox = wanted in ("", "auto", "all", "firefox")

    for label, user_data in _chromium_candidates(wanted):
        profiles = _profile_dirs(user_data)
        if not profiles:
            continue
        try:
            key = _chromium_key(user_data)
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"{label}：取密钥失败（{type(exc).__name__}: {exc}）")
            continue
        for profile in profiles:
            db = _cookie_db(profile)
            if db is None:
                continue
            try:
                values, note = _read_chromium_db(db, key)
            except PermissionError:
                reasons.append(f"{label}（{profile.name}）：浏览器正在运行，Cookie 库被占用")
                continue
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"{label}（{profile.name}）：读取失败（{type(exc).__name__}）")
                continue
            if values.get("sessdata"):
                logger.info(
                    f"[cookie] 命中 {label}（{profile.name}）：字段 {sorted(values)}"
                    + (f"（{note}）" if note else "")
                )
                return CookieScanResult(values=values, source=f"{label}/{profile.name}")
            if note:
                reasons.append(f"{label}（{profile.name}）：{note}")

    if wants_firefox:
        for db in _firefox_candidates():
            try:
                values, _note = _read_firefox_db(db)
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"Firefox：读取失败（{type(exc).__name__}）")
                continue
            if values.get("sessdata"):
                logger.info(f"[cookie] 命中 Firefox：字段 {sorted(values)}")
                return CookieScanResult(values=values, source=f"Firefox/{db.parent.name}")

    reason = "；".join(reasons) if reasons else "本机没有找到可读的 B 站登录 Cookie"
    logger.info(f"[cookie] 未找到可用登录态：{reason}")
    return CookieScanResult(reason=reason)
