# -*- coding: utf-8 -*-
"""后端安全与健壮性修复的回归测试

覆盖：bootstrap version 字段、静态文件扩展名白名单、
JWT UA 指纹绑定、统一 DELETE 通道后端语义（服务端侧不变）、
登录限流字典上限（只淘汰过期条目、不丢生效中的锁定）、
请求体大小上限、ChatReq 消息长度上限、HSTS/CSP 响应头、
DR 业务范围校验、上传临时文件链路。
不触发 MILP 求解——全部秒级。
"""
import io
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

import server  # noqa: E402


@pytest.fixture(scope="module")
def client():
    server.AUTH.set_password("test-password-123")
    return TestClient(server.app)


def _login(client):
    # AuthStore 单账户：用户名必须取真实账户名（"tester" 是限流测试专用的错误账号）
    return client.post("/api/auth/login",
                       json={"username": server.AUTH.username, "password": "test-password-123"})


def _auth_headers(client):
    r = _login(client)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ---------- 版本号单一事实来源 ----------
def test_bootstrap_contains_version(client):
    r = client.get("/api/bootstrap", headers=_auth_headers(client))
    assert r.status_code == 200
    from src import __version__
    assert r.json()["version"] == __version__


# ---------- 静态文件扩展名白名单 ----------
def test_static_serves_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "汐储" in r.text


def test_static_blocks_non_whitelisted_extension(client):
    # 模拟敏感文件被误放进 web/ 的情况：非白名单扩展名必须 403
    r = client.get("/fake-secrets.json")
    assert r.status_code == 403
    r2 = client.get("/fake-module.pkl")
    assert r2.status_code == 403


# ---------- HSTS / CSP 响应头 ----------
def test_security_headers_present(client):
    r = client.get("/")
    assert r.headers.get("strict-transport-security") == "max-age=31536000; includeSubDomains"
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "cdn.jsdelivr.net" not in csp  # vendor 已本地化，CSP 不再放行外部 CDN


# ---------- JWT 绑定 UA 指纹 ----------
def test_token_bound_to_user_agent(client):
    tok = _login(client).json()["token"]
    # 同一 UA（TestClient 默认 testclient）→ 放行
    ok = client.get("/api/bootstrap", headers={"Authorization": f"Bearer {tok}"})
    assert ok.status_code == 200
    # 换 UA（模拟 token 被窃取后在其他环境使用）→ 401
    stolen = client.get("/api/bootstrap",
                        headers={"Authorization": f"Bearer {tok}", "User-Agent": "evil-client"})
    assert stolen.status_code == 401


# ---------- 登录限流字典上限（只淘汰过期条目，不丢生效中的锁定） ----------
def test_login_gate_dicts_bounded():
    """灌满表后长度必须有界——且**不能**把生效中的锁定淘汰掉。

    旧实现是纯插入序 FIFO（`pop(next(iter(d)))`），存在一条真实的锁定绕过链：
    攻击者用 1 个 IP 轮换海量不同 username 打 /api/auth/login，灌满 10000 条后
    FIFO 会把**最旧的**条目挤出去——而 admin 的锁定记录恰好最旧，于是"已锁定"
    被洗成"未锁定"，限流形同虚设。所以这里断言的不是"长度有界"，而是
    "长度有界 **且** admin 仍被锁着"。
    """
    server._LOGIN_FAILS.clear()
    server._LOGIN_LOCKED_UNTIL.clear()
    try:
        # 先把 admin 锁上
        for _ in range(server._LOGIN_MAX_FAILS):
            server._login_record_fail("9.9.9.9|admin")
        assert server._login_rate_limited("9.9.9.9|admin") is not None

        for i in range(server._LOGIN_MAX_ENTRIES + 500):
            server._login_record_fail(f"1.2.3.4|user{i}")

        assert len(server._LOGIN_FAILS) <= server._LOGIN_HARD_MAX_ENTRIES
        assert len(server._LOGIN_LOCKED_UNTIL) <= server._LOGIN_HARD_MAX_ENTRIES
        # 关键回归：锁定记录没有被"洗掉"
        assert server._login_rate_limited("9.9.9.9|admin") is not None
    finally:
        server._LOGIN_FAILS.clear()
        server._LOGIN_LOCKED_UNTIL.clear()


def test_login_gate_prunes_expired_entries():
    """已过期的条目可以被淘汰（否则表只进不出，长跑必涨内存）。"""
    server._LOGIN_FAILS.clear()
    server._LOGIN_LOCKED_UNTIL.clear()
    try:
        for i in range(server._LOGIN_MAX_ENTRIES + 10):
            server._login_record_fail(f"5.6.7.8|stale{i}")
        # 把一半条目标成"锁定已过期"
        now = server.time.time()
        for k in list(server._LOGIN_LOCKED_UNTIL)[:5000]:
            server._LOGIN_LOCKED_UNTIL[k] = now - 1
        server._login_record_fail("5.6.7.8|fresh")
        assert len(server._LOGIN_FAILS) <= server._LOGIN_MAX_ENTRIES
    finally:
        server._LOGIN_FAILS.clear()
        server._LOGIN_LOCKED_UNTIL.clear()


# ---------- 请求体大小上限（未认证的大 body 打不到鉴权逻辑） ----------
def test_body_limit_rejects_oversized_unauthenticated_request(client):
    big = b'{"message": "' + b"x" * (2 * 1024 * 1024) + b'"}'
    r = client.post("/api/chat", content=big,
                    headers={"content-type": "application/json"})
    # 413 而不是 401：中间件在鉴权之前就拒了，连 body 都没读
    assert r.status_code == 413, r.text
    assert "1MB" in r.json()["detail"]


def test_body_limit_leaves_normal_requests_alone(client):
    assert client.get("/api/system-info").status_code == 200
    r = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401


def test_chat_message_length_is_capped(client):
    """ChatReq.message 的 8000 字符上限——pydantic 在解析阶段就拒。"""
    h = _auth_headers(client)
    r = client.post("/api/chat", headers=h, json={"message": "x" * 8001})
    assert r.status_code == 422
    assert any(d.get("type") == "string_too_long" for d in r.json()["detail"])


# ---------- DR 业务范围校验 ----------
def test_dr_trigger_rejects_out_of_range(client):
    h = _auth_headers(client)
    # target 超上限
    r = client.post("/api/dr/trigger", headers=h,
                    json={"start": "15:00", "end": "17:00", "target": 1e9, "subsidy": 0.8, "dr_type": "削峰"})
    assert r.status_code == 400 and "10000" in r.json()["detail"]
    # subsidy 超范围
    r = client.post("/api/dr/trigger", headers=h,
                    json={"start": "15:00", "end": "17:00", "target": 400, "subsidy": 99, "dr_type": "削峰"})
    assert r.status_code == 400 and "20" in r.json()["detail"]
    # 时段超 8 小时
    r = client.post("/api/dr/trigger", headers=h,
                    json={"start": "08:00", "end": "20:00", "target": 400, "subsidy": 0.8, "dr_type": "削峰"})
    assert r.status_code == 400 and "8" in r.json()["detail"]
    # 合法值可入队（随后清掉，不污染其他测试）
    r = client.post("/api/dr/trigger", headers=h,
                    json={"start": "15:00", "end": "17:00", "target": 400, "subsidy": 0.8, "dr_type": "削峰"})
    assert r.status_code == 200 and r.json()["ok"] is True
    server._DEFAULT_SESSION.manual_dr_signals.clear()


# ---------- 上传走临时文件链路（正常解析 + 超限拒绝） ----------
def _csv_bytes(n_rows=96):
    import numpy as np
    import pandas as pd
    ts = pd.date_range("2026-01-01", periods=n_rows, freq="15min")
    df = pd.DataFrame({
        "timestamp": ts, "load_kw": np.linspace(500, 800, n_rows),
        "price": np.full(n_rows, 0.8), "temp": np.full(n_rows, 25.0),
    })
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode()


def test_upload_small_csv_ok(client):
    h = _auth_headers(client)
    r = client.post("/api/upload", headers=h,
                    files={"file": ("data.csv", io.BytesIO(_csv_bytes()), "text/csv")})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    # 还原内置演示数据，不污染其他测试
    client.delete("/api/upload", headers=h)


def test_upload_content_length_preflight(client):
    h = _auth_headers(client)
    r = client.post("/api/upload", headers={**h, "Content-Length": str(60 * 1024 * 1024)},
                    files={"file": ("big.csv", io.BytesIO(b"x"), "text/csv")})
    assert r.status_code == 400 and "50MB" in r.json()["msg"]


# ---------- 数据指纹改 SHA256 ----------
def test_data_fingerprint_sha256():
    import pandas as pd
    df = pd.DataFrame({"a": [1, 2, 3]})
    fp = server._data_fingerprint(df)
    assert len(fp) == 32 and all(c in "0123456789abcdef" for c in fp)


# ---------- 自动文档默认关闭（P2.7） ----------
def test_auto_docs_are_not_mounted_by_default(client):
    """默认配置下 /docs、/redoc、/openapi.json 都不该可达。

    这三条路径不在 /api/ 前缀下，此前既不经过 JWT 中间件、也不在静态白名单里，
    未认证就能拿到全部 28 个端点的签名、参数与响应模型。现在默认不挂载。
    注意断言写 in (403, 404) 而不是 == 404：/openapi.json 的 .json 后缀会被
    静态挂载的扩展名白名单先拦成 403，两者都算"不可达"，钉死具体码会把
    静态白名单的实现细节耦进这条测试。
    """
    if server._ENABLE_DOCS:  # 有人显式开了文档，这条前提不成立
        pytest.skip("ENERGY_ENABLE_DOCS 已开启，默认关闭语义不适用")
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(path)
        assert r.status_code in (403, 404), f"{path} 默认应当不可达，实际 {r.status_code}"


def test_auto_docs_paths_are_auth_gated_when_enabled():
    """开启 ENERGY_ENABLE_DOCS 后必须鉴权——不能退回"未认证可读"。

    这条不重开应用（_ENABLE_DOCS 在 import 时读环境变量），而是直接检查中间件的
    判定集合与开关语义：_DOCS_PATHS 覆盖四条文档路径，且鉴权判定在开关为真时
    才把它们纳入。这样即使将来有人把 _DOCS_PATHS 里的项删掉，这里也会红。
    """
    assert server._DOCS_PATHS == {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
    # 文档页 CSP 必须允许 jsdelivr，否则 Swagger UI 白屏（这正是此前 /docs 打不开的原因）
    assert "cdn.jsdelivr.net" in server._DOCS_CSP
    # 全局 CSP 保持零外部 CDN
    assert "cdn.jsdelivr.net" not in server._SECURITY_HEADERS["Content-Security-Policy"]
