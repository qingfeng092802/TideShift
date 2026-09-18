# -*- coding: utf-8 -*-
"""后端安全与健壮性修复的回归测试

覆盖：bootstrap version 字段、静态文件扩展名白名单、
JWT UA 指纹绑定、统一 DELETE 通道后端语义（服务端侧不变）、
登录限流字典 LRU 上限、HSTS/CSP 响应头、
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


# ---------- 登录限流字典 LRU 上限 ----------
def test_login_gate_dicts_bounded():
    for i in range(server._LOGIN_MAX_ENTRIES + 500):
        server._login_record_fail(f"1.2.3.4|user{i}")
    assert len(server._LOGIN_FAILS) <= server._LOGIN_MAX_ENTRIES
    assert len(server._LOGIN_LOCKED_UNTIL) <= server._LOGIN_MAX_ENTRIES
    server._LOGIN_FAILS.clear()
    server._LOGIN_LOCKED_UNTIL.clear()


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
    import pandas as pd
    import numpy as np
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
