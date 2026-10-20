"""B 站网页扫码登录：自己走一遍 generate → poll → 换票，比库里的解析更抗改版。

为什么不用 `bilibili_api.login_v2.QrCodeLogin`？2026 年 B 站改了扫码后的发券流程：
轮询返回的 `url` 里不再直接带 `SESSDATA`，而是给一个带 `ticket` 的**跨域确认地址**，
要再访问那个地址、从响应的 `Set-Cookie` 里才能拿到真正的登录态。旧库仍然按
`url.split("?")[1]` 找 SESSDATA，于是「手机提示登录成功、凭据却是空的」——
2026-10-20 真机就是这么翻车的（日志里 `登录成功了但没拿到 SESSDATA`）。

本模块把三步都写清楚：
1. `request_qr()`：向 passport 要二维码链接 + qrcode_key，并用 qrcode 库渲染成 PNG；
2. `poll_once()`：轮询状态（86101 未扫 / 86090 待确认 / 86038 过期 / 0 成功）；
3. `credential_from_payload()`：成功时先按「老流程 URL 里带 Cookie」解析；不行就
   拿 `ticket` 去跨域地址换票，从 `Set-Cookie` 收集；再不行看 `cookie_info`。

安全口径：本模块的日志只写「走到了哪一步、拿到了哪些 cookie 名」，**绝不写值**。
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("bili_sharer_login")

GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
# 换票地址：官方在不同端用过几个域名/路径，按顺序试，谁先给 SESSDATA 就收工
CROSS_DOMAIN_URLS = (
    "https://passport.biligame.com/x/passport-login/web/crossDomain",
    "https://passport.bilibili.com/x/passport-login/web/crossDomain",
    "https://passport.biligame.com/crossDomain",
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REFERER = "https://www.bilibili.com/"
REQUEST_TIMEOUT = 15.0
# 轮询状态码（B 站 passport 的口径）
CODE_SCAN = 86101
CODE_CONFIRM = 86090
CODE_TIMEOUT = 86038
CODE_SUCCESS = 0
# 需要的 cookie 名 → 插件内部字段名
WANTED_COOKIES = {
    "SESSDATA": "sessdata",
    "bili_jct": "bili_jct",
    "buvid3": "buvid3",
    "DedeUserID": "dedeuserid",
    "ac_time_value": "ac_time_value",
}


class BiliLoginError(Exception):
    """扫码登录流程里的可读失败：接口变化 / 网络 / 返回结构不对。"""


@dataclass
class QrChallenge:
    """一次扫码会话：二维码内容、轮询用的 key、可直接发出去的 PNG。"""

    url: str
    key: str
    png: bytes


def _client() -> httpx.AsyncClient:
    """建一个带 UA / Referer 的客户端；不缓存 cookie，每步都显式处理。"""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=REQUEST_TIMEOUT),
        headers={"User-Agent": UA, "Referer": REFERER},
        follow_redirects=True,
    )


def new_client() -> httpx.AsyncClient:
    """对外暴露的客户端工厂（调用方用它复用连接池轮询）。"""
    return _client()


def render_qr_png(content: str) -> bytes:
    """把二维码内容渲染成 PNG 字节（qrcode 是 bilibili-api-python 的依赖，必然在）。"""
    try:
        import qrcode  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise BiliLoginError(f"缺少 qrcode 库，二维码画不出来：{type(exc).__name__}") from exc
    image = qrcode.make(content)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def request_qr(client: httpx.AsyncClient | None = None) -> QrChallenge:
    """要一张新二维码；接口异常/结构变化都包装成 BiliLoginError。

    client 建议由调用方传进来并复用（generate → poll → 换票同一会话，
    B 站会把 buvid 之类的会话 cookie 串起来；换票时少了它容易失败）。
    """
    owns_client = client is None
    if client is None:
        client = _client()
    try:
        try:
            response = await client.get(GEN_URL, params={"source": "main-fe-header"})
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise BiliLoginError(f"请求二维码接口失败：{type(exc).__name__}: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    url = str(data.get("url") or "")
    key = str(data.get("qrcode_key") or "")
    if payload.get("code") != 0 or not url or not key:
        raise BiliLoginError(
            f"二维码接口返回异常：code={payload.get('code')} message={payload.get('message')}"
        )
    return QrChallenge(url=url, key=key, png=render_qr_png(url))


async def poll_once(client: httpx.AsyncClient, key: str) -> tuple[str, dict[str, Any]]:
    """查一次扫码状态。

    Returns:
        (状态, data)：状态取 `scan` / `confirm` / `timeout` / `success` / `unknown`；
        只有 `success` 时 data 里才有换取凭据要用的 `url` / `refresh_token`。
    """
    try:
        response = await client.get(POLL_URL, params={"qrcode_key": key, "source": "main-fe-header"})
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return "unknown", {"error": f"{type(exc).__name__}: {exc}"}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    code = data.get("code")
    if code == CODE_SCAN:
        return "scan", data
    if code == CODE_CONFIRM:
        return "confirm", data
    if code == CODE_TIMEOUT:
        return "timeout", data
    if code == CODE_SUCCESS:
        return "success", data
    return "unknown", data


def _query_pairs(query_text: str) -> list[tuple[str, str]]:
    """把查询串拆成 (名, 原样值) 列表——**不做 URL 解码**。

    为什么不用 parse_qs？它会把 `%2C` 还原成逗号，而 Cookie 头里的逗号是分隔符：
    SESSDATA 必须保持百分号编码（浏览器存的就是编码后的样子），否则发出去就是坏 cookie。
    """
    pairs: list[tuple[str, str]] = []
    for part in str(query_text or "").split("&"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name and value.strip():
            pairs.append((name, value.strip()))
    return pairs


def parse_credential_url(url: str) -> dict[str, str]:
    """从「带 Cookie 的 URL」里解析出需要的字段（老流程用）。

    B 站给过两种形态：完整 URL（`https://...?SESSDATA=...&bili_jct=...`）和
    只有查询串的裸串；这里都接得住，值保持百分号编码原样。
    """
    text = str(url or "").strip()
    if not text:
        return {}
    if "?" in text:
        query_text = text.split("?", 1)[1]
    elif "=" in text:
        query_text = text
    else:
        return {}
    values: dict[str, str] = {}
    for name, value in _query_pairs(query_text):
        field = WANTED_COOKIES.get(name)
        if field and field not in values:
            values[field] = value
    return values


def _map_cookie_name(name: str) -> str:
    """Set-Cookie / cookie_info 里的名字 → 插件字段名；不认识的返回空串。"""
    direct = WANTED_COOKIES.get(name)
    if direct:
        return direct
    lowered = name.lower()
    for cookie_name, field in WANTED_COOKIES.items():
        if cookie_name.lower() == lowered:
            return field
    return ""


def collect_set_cookies(response: httpx.Response) -> dict[str, str]:
    """从一次（含重定向链的）响应里收集 Set-Cookie；只留我们要的那几个字段。"""
    values: dict[str, str] = {}
    sources = list(getattr(response, "history", []) or []) + [response]
    for item in sources:
        try:
            cookies = item.headers.get_list("set-cookie")
        except Exception:  # noqa: BLE001
            cookies = []
        for raw in cookies:
            pair = str(raw).split(";", 1)[0]
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            field = _map_cookie_name(name.strip())
            if field and value.strip():
                values[field] = value.strip()
    return values


def _cookies_from_info(data: dict[str, Any]) -> dict[str, str]:
    """兜底：少数返回会在 `cookie_info.cookies` 里给出结构化 cookie 列表。"""
    values: dict[str, str] = {}
    info = data.get("cookie_info") if isinstance(data.get("cookie_info"), dict) else {}
    for item in info.get("cookies") or []:
        if not isinstance(item, dict):
            continue
        field = _map_cookie_name(str(item.get("name") or ""))
        value = str(item.get("value") or "").strip()
        if field and value:
            values[field] = value
    return values


def _sso_links(text: str) -> list[str]:
    """换票响应里偶尔还会给一个 sso 链接，跟着它走才能拿到 cookie（限 b 站域名）。"""
    import re  # noqa: PLC0415

    links: list[str] = []
    for match in re.findall(r"https?://[^\"'\s<>\\]+", str(text or "")):
        link = match.replace("\\u0026", "&").replace("&amp;", "&")
        host = (urlparse(link).hostname or "").lower()
        if ("bilibili" in host or "biligame" in host) and link not in links:
            links.append(link)
    return links[:2]


async def exchange_ticket(client: httpx.AsyncClient, ticket: str, gourl: str = "") -> dict[str, str]:
    """拿跨域 ticket 换真实 Cookie（2026 年新流程的关键一步）。

    Returns:
        收集到的字段（可能没有 sessdata，由调用方判断并给出可读失败）。
    """
    target = gourl or "https://www.bilibili.com"
    for base in CROSS_DOMAIN_URLS:
        try:
            response = await client.get(
                base, params={"ticket": ticket, "gourl": quote(target, safe="")}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[登录] 换票请求失败（{urlparse(base).netloc}）：{type(exc).__name__}: {exc}")
            continue
        values = collect_set_cookies(response)
        if values.get("sessdata"):
            logger.info(f"[登录] 换票成功（{urlparse(base).netloc}）：字段 {sorted(values)}")
            return values
        # 有的实现会先回一段 JSON/HTML，里面再给一个 sso 地址
        for link in _sso_links(getattr(response, "text", "") or ""):
            try:
                extra = await client.get(link)
            except Exception:  # noqa: BLE001
                continue
            values.update(collect_set_cookies(extra))
            if values.get("sessdata"):
                logger.info(f"[登录] 跟随 sso 链接后换票成功：字段 {sorted(values)}")
                return values
        logger.info(f"[登录] 换票未拿到 SESSDATA（{urlparse(base).netloc}），换个地址再试")
    return {}


async def credential_from_payload(
    data: dict[str, Any], client: httpx.AsyncClient | None = None
) -> tuple[dict[str, str], str]:
    """把「扫码成功」的返回翻译成凭据字段，返回 (字段, 走的哪条路)。

    路线标记：`url`（老流程，URL 里直接带 Cookie）/ `ticket`（新流程，跨域换票）/
    `cookie_info`（结构化兜底）/ `empty`（都没拿到）。
    """
    refresh = str(data.get("refresh_token") or "")
    url = str(data.get("url") or "")

    values = parse_credential_url(url)
    if values.get("sessdata"):
        if refresh:
            values.setdefault("ac_time_value", refresh)
        return values, "url"

    query = parse_qs(url.split("?", 1)[1]) if "?" in url else {}
    ticket = str((query.get("ticket") or [""])[0] or "")
    if ticket:
        gourl = str((query.get("gourl") or ["https://www.bilibili.com"])[0] or "https://www.bilibili.com")
        owns_client = client is None
        if client is None:
            client = _client()
        try:
            exchanged = await exchange_ticket(client, ticket, gourl)
        finally:
            if owns_client:
                await client.aclose()
        if exchanged.get("sessdata"):
            if refresh:
                exchanged.setdefault("ac_time_value", refresh)
            return exchanged, "ticket"
        fallback = _cookies_from_info(data)
        if fallback.get("sessdata"):
            if refresh:
                fallback.setdefault("ac_time_value", refresh)
            return fallback, "cookie_info"
        return {}, "ticket"

    fallback = _cookies_from_info(data)
    if fallback.get("sessdata"):
        if refresh:
            fallback.setdefault("ac_time_value", refresh)
        return fallback, "cookie_info"
    return {}, "empty"


def describe_payload(data: dict[str, Any]) -> str:
    """给日志/回话用的一句话：只说有哪些键，**不打印值**。"""
    keys = sorted(str(key) for key in (data or {}).keys())
    url = str(data.get("url") or "")
    has_ticket = "ticket=" in url
    return f"字段={keys} url={'有' if url else '空'} ticket={'有' if has_ticket else '无'}"


def now() -> float:
    """单调时钟的薄封装（测试想拨表就替换本模块属性）。"""
    return time.monotonic()
