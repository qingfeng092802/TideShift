# -*- coding: utf-8 -*-
"""LLM Base URL SSRF 防护（公共模块）

P0-11 修复：此前 _assert_safe_llm_url 只存在于 backend/server.py，
Streamlit 侧 app.py 的 _test_llm_connection 直连任意 URL 无防护。
本模块下沉为 src/utils 公共实现，两个前端强制接入。

规则：
- 仅允许 http/https 协议、常规端口（80/443）
- 拒绝内网/链路本地/元数据地址（黑名单方式，白名单会误伤自建代理网关）
- 环境变量 LLM_TEST_ALLOW_HOSTS 提供逃生口（逗号分隔额外放行主机名）

统一抛 SSRFBlockedError（不依赖 FastAPI），由调用方决定如何呈现给用户。
"""
import os
import socket
from urllib.parse import urlparse


class SSRFBlockedError(ValueError):
    """Base URL 未通过 SSRF 安全校验"""


def assert_safe_llm_url(base: str):
    """校验 LLM Base URL，不通过时抛 SSRFBlockedError（message 面向用户展示）。"""
    try:
        u = urlparse(base)
    except Exception:
        raise SSRFBlockedError("Base URL 格式非法")
    if u.scheme not in ("http", "https"):
        raise SSRFBlockedError("仅允许 http/https 协议")
    host = (u.hostname or "").lower()
    if not host:
        raise SSRFBlockedError("Base URL 缺少主机名")
    if u.port and u.port not in (80, 443):
        raise SSRFBlockedError("仅允许 80/443 端口")
    allow = {h.strip().lower() for h in os.getenv(
        "LLM_TEST_ALLOW_HOSTS", "").split(",") if h.strip()}
    if host in allow or host.endswith(".deepseek.com") or host.endswith(".openai.com") \
            or host.endswith(".anthropic.com"):
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise SSRFBlockedError(f"主机解析失败: {host}")
    for info in infos:
        ip = info[4][0]
        if ":" in ip:
            bad = ip == "::1" or ip.lower().startswith(("fc", "fd", "fe80"))
        else:
            parts = [int(x) for x in ip.split(".")]
            bad = (parts[0] == 10 or parts[0] == 127 or parts[0] == 169 and parts[1] == 254
                   or parts[0] == 172 and 16 <= parts[1] <= 31
                   or parts[0] == 192 and parts[1] == 168
                   or parts[0] == 0)
        if bad:
            raise SSRFBlockedError("禁止访问内网/保留地址（SSRF 防护）")
