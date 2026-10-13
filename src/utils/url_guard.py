# -*- coding: utf-8 -*-
"""LLM Base URL SSRF 防护（公共模块）

存在理由：这个校验原先只长在 backend/server.py 里，另一条「测试连接」的
路径直连用户填的任意 URL，等于一条现成的 SSRF 入口。
下沉为 src/utils 公共实现后，所有调用方都必须过它。

规则：
- 仅允许 http/https 协议、常规端口（80/443）
- 解析出的每个 IP 都必须是**公网地址**（用 `ipaddress.is_global`，而不是手写网段黑名单）
- 环境变量 LLM_TEST_ALLOW_HOSTS 提供逃生口（逗号分隔额外放行主机名，
  允许它们指向内网——自建网关就是这个用法）

P1.2 修复（原实现在 `:75-82` 手写网段黑名单，漏掉一批）：
- IPv6 分支只查 `::1` 和 `fc/fd/fe80` 前缀 → **`::ffff:127.0.0.1`（IPv4-mapped）
  与 `::` 直接绕过**。现改为解包 IPv4-mapped / 6to4 / Teredo 后按 IPv4 判定。
- IPv4 分支漏 `100.100.100.200`（云厂商元数据）、`100.64.0.0/10`（CGNAT）、
  `198.18.0.0/15`（基准测试段）、`192.0.0.0/24`、`198.51.100.0/24` 等。
  现改为 `not addr.is_global` —— 白名单语义，只有公网地址放行。
- **DNS rebinding**：原实现只校验 `getaddrinfo` 的结果，随后 `urlopen` 会
  **再解析一次**；攻击者用 TTL=0 的域名即可让第二次解析落到内网。
  现由 `safe_urlopen()` 把连接**钉死在已校验的那个 IP** 上（Host 头仍是原主机名，
  TLS 仍按原主机名校验 SNI/证书），并拒绝跟随重定向。

统一抛 SSRFBlockedError（不依赖 FastAPI），由调用方决定如何呈现给用户。
"""
import http.client
import ipaddress
import os
import socket
import ssl
import urllib.error
import urllib.request
from typing import List, Optional
from urllib.parse import urlparse, urlunsplit

# 只允许的端口（LLM API 的常规端口）
_ALLOWED_PORTS = (80, 443)

# 内置放行域名：这几家官方 API 不可能是内网地址，跳过 DNS 校验省一次解析。
# 注意：仍然会钉 IP、仍然拒绝重定向——放行的只是"IP 必须公网"这一条。
_BUILTIN_ALLOW_SUFFIXES = (".deepseek.com", ".openai.com", ".anthropic.com")


class SSRFBlockedError(ValueError):
    """Base URL 未通过 SSRF 安全校验"""


def normalize_llm_base_url(base: str) -> str:
    """归一化 OpenAI 兼容 Base URL：去掉尾部斜杠，**主机根路径自动补 `/v1`**。

    只补"路径为空"这一种确定情形。用户显式写了路径就原样保留——
    `/compatible-mode/v1`（DashScope）、`/api/llm/v3`（自建网关）、
    `/openai/deployments/xxx`（Azure）各自形状不同，猜不得。

    存在理由：DeepSeek 官方文档同时给出 `https://api.deepseek.com` 和
    `https://api.deepseek.com/v1` 两种写法，而 OpenAI SDK 是**直接拼接**
    `base_url + "/chat/completions"`，不会自动补 `/v1`。用户按前者填，
    得到的是 404，界面上只写着"失败"，看不出是路径少了一段。

    不校验协议/主机（那是 assert_safe_llm_url 的职责），也不抛异常：
    解析不出主机名就原样返回，让下游的 SSRF 校验去给最终用户报错。
    """
    url = (base or "").strip().rstrip("/")
    if not url:
        return url
    try:
        u = urlparse(url)
    except ValueError:
        return url
    if not u.netloc or (u.path and u.path != "/"):
        return url
    return urlunsplit((u.scheme, u.netloc, "/v1", "", ""))


def _allowed_hosts() -> set:
    """环境变量逃生口：逗号分隔的额外放行主机名。"""
    return {h.strip().lower() for h in os.getenv("LLM_TEST_ALLOW_HOSTS", "").split(",")
            if h.strip()}


def _is_allowlisted(host: str) -> bool:
    if host in _allowed_hosts():
        return True
    return any(host.endswith(s) for s in _BUILTIN_ALLOW_SUFFIXES)


def _ip_is_blocked(ip_str: str) -> bool:
    """判断一个 IP 是否禁止访问。白名单语义：只有公网地址放行。

    IPv4-mapped / 6to4 / Teredo 会先解包出内嵌的 IPv4 再判定——
    否则 `::ffff:127.0.0.1` 这类地址会被当成"普通的公网 IPv6"放过去。
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True                      # 解析不出来 → 视为不可信
    if addr.version == 6:
        for embedded in (getattr(addr, "ipv4_mapped", None),
                         getattr(addr, "sixtofour", None)):
            if embedded is not None:
                return _ip_is_blocked(str(embedded))
        teredo = getattr(addr, "teredo", None)
        if teredo is not None:
            return _ip_is_blocked(str(teredo[1]))   # (server, client) → client 是内嵌 IPv4
    if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return True
    return not addr.is_global


def resolve_and_validate(host: str, allow_private: bool = False) -> List[str]:
    """解析主机并校验全部 IP，返回可用的 IP 列表（已按"公网"过滤过）。

    任一解析结果不合法即整体拒绝（不做"挑一个合法的用"）——一台主机同时解析出
    公网与内网地址时，攻击者可以控制返回顺序，挑着用等于没校验。
    `allow_private=True` 用于白名单主机（自建网关场景），此时只做解析与钉 IP。
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        # 保留原始 OSError 作为 __cause__：面向用户的 message 是统一口径，
        # 但日志里需要看到"Name or service not known"这类底层原因才能定位。
        raise SSRFBlockedError(f"主机解析失败: {host}") from exc
    ips: List[str] = []
    for info in infos:
        # sockaddr 的元素类型是 str | int（IPv4/IPv6 恒为 str，但元组里混着端口号），
        # getaddrinfo(host, None, ...) 的第 0 位一定是地址字符串，显式转一下让类型收敛。
        ip = str(info[4][0])
        if not allow_private and _ip_is_blocked(ip):
            raise SSRFBlockedError("禁止访问内网/保留地址（SSRF 防护）")
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise SSRFBlockedError(f"主机解析失败: {host}")
    return ips


def assert_safe_llm_url(base: str) -> str:
    """校验 LLM Base URL，不通过时抛 SSRFBlockedError（message 面向用户展示）。

    返回**已校验的 IP**，供 safe_urlopen 钉连接用（避免二次解析被 rebinding 利用）。
    """
    try:
        u = urlparse(base)
    except ValueError as exc:
        raise SSRFBlockedError("Base URL 格式非法") from exc
    if u.scheme not in ("http", "https"):
        raise SSRFBlockedError("仅允许 http/https 协议")
    host = (u.hostname or "").lower()
    if not host:
        raise SSRFBlockedError("Base URL 缺少主机名")
    if u.port and u.port not in _ALLOWED_PORTS:
        raise SSRFBlockedError("仅允许 80/443 端口")
    ips = resolve_and_validate(host, allow_private=_is_allowlisted(host))
    return ips[0]


# ---------------------------------------------------------------------------
# 钉 IP 的连接：校验与建连之间不能再解析一次，也不能经代理（代理会自己解析）
# ---------------------------------------------------------------------------

def _pinned_http_class(ip: str):
    class _PinnedHTTPConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection(
                (ip, self.port), self.timeout, self.source_address)

    return _PinnedHTTPConnection


def _pinned_https_class(ip: str, host: str, context: Optional[ssl.SSLContext]):
    ctx = context or ssl.create_default_context()

    class _PinnedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection(
                (ip, self.port), self.timeout, self.source_address)
            # server_hostname 用原主机名：TLS 的 SNI 与证书校验仍按域名走，
            # 钉的只是"连到哪个 IP"。
            self.sock = ctx.wrap_socket(sock, server_hostname=host)

    return _PinnedHTTPSConnection


class _PinnedResponse:
    """`http.client` 响应的最小包装，形状对齐 `urlopen` 的返回（`with ... as r: r.read()`）。"""

    def __init__(self, conn, resp):
        self._conn, self._resp = conn, resp
        self.status, self.headers = resp.status, resp.headers

    def read(self, *a, **kw):
        return self._resp.read(*a, **kw)

    def getcode(self):
        return self._resp.status

    def geturl(self):
        return self._resp.geturl() if hasattr(self._resp, "geturl") else ""

    def close(self):
        try:
            self._resp.close()
        finally:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def safe_urlopen(request, timeout: float = 15.0):
    """在 SSRF 校验之后发起请求：**连接钉死在已校验的 IP 上，且不跟随重定向**。

    与 `urllib.request.urlopen` 的用法一致（`with ... as resp: resp.read()`），
    调用方无需关心钉 IP 的细节。校验不通过时抛 SSRFBlockedError。

    为什么不用 `build_opener()`：`build_opener` 会把**默认的**
    `HTTPHandler/HTTPSHandler` 放在前面，后 `add_handler` 的钉 IP 处理器永远轮不到；
    更糟的是默认 `ProxyHandler` 会读系统代理（实测本机
    `getproxies() == {'http': 'http://127.0.0.1:4780', ...}`），
    请求交给代理后由**代理自己解析域名**，钉 IP 与重定向控制同时失效。
    直接用 `http.client` 才能把"连哪个 IP、发什么、跟不跟跳转"三件事都攥在手里。
    """
    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if not host:
        raise SSRFBlockedError("Base URL 缺少主机名")
    if u.scheme not in ("http", "https"):
        raise SSRFBlockedError("仅允许 http/https 协议")
    # 重新校验一次：调用方可能在校验与请求之间改过 URL
    ip = assert_safe_llm_url(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query

    method = "GET"
    data = None
    headers = {}
    if isinstance(request, urllib.request.Request):
        method = request.get_method()
        data = request.data
        headers = dict(request.header_items())
    headers.setdefault("Host", host if port in (80, 443) else f"{host}:{port}")

    if u.scheme == "https":
        conn = _pinned_https_class(ip, host, ssl.create_default_context())(
            host, port, timeout=timeout)
    else:
        conn = _pinned_http_class(ip)(host, port, timeout=timeout)

    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
    except SSRFBlockedError:
        conn.close()
        raise
    except Exception:
        conn.close()
        raise
    if resp.status in (301, 302, 303, 307, 308):
        loc = resp.getheader("Location") or ""
        conn.close()
        raise SSRFBlockedError(f"拒绝跟随重定向（HTTP {resp.status} → {loc}）")
    return _PinnedResponse(conn, resp)
