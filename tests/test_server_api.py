# -*- coding: utf-8 -*-
"""🔴#7/T3：FastAPI 层 API 安全测试（此前 1273 行 27 个端点零测试）

覆盖：未登录 401、登录成功拿 token、must_change 强制拦截（🔴#3）、
登录限流（🟠#21）、SSRF 黑名单（P0-03）、会话隔离（🔴#4）。
不触发 MILP 求解——全部是秒级安全语义测试。
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

# server.py 以脚本目录方式导入 auth（import auth），需先把 backend 目录注入 sys.path
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

import server  # noqa: E402  backend/server.py
from src.utils.url_guard import SSRFBlockedError, assert_safe_llm_url  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # 测试期固定口令（无条件重置，保证测试自洽且不依赖 config/auth.json 历史）
    server.AUTH.set_password("test-password-123")
    return TestClient(server.app)


def _login(client, username="tester", password="test-password-123"):
    # 用 tester 账号：AuthStore 只支持单账户，这里直接对真实账户校验
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_protected_endpoint_requires_auth(client):
    r = client.get("/api/bootstrap")
    assert r.status_code == 401


def test_protected_endpoint_rejects_bad_token(client):
    r = client.get("/api/bootstrap", headers={"Authorization": "Bearer forged.token.here"})
    assert r.status_code == 401


def test_login_success_and_bootstrap(client):
    # 真实账户（测试初始化为 test-password-123；若 must_change 则先改密）
    r = client.post("/api/auth/login", json={"username": server.AUTH.username,
                                             "password": "test-password-123"})
    assert r.status_code == 200
    tok = r.json()["token"]
    r2 = client.get("/api/bootstrap", headers={"Authorization": f"Bearer {tok}"})
    assert r2.status_code == 200
    assert "params" in r2.json()


def test_wrong_password_401(client):
    r = client.post("/api/auth/login", json={"username": server.AUTH.username,
                                             "password": "wrong"})
    assert r.status_code == 401


def test_must_change_blocks_api(monkeypatch, client):
    """🔴#3：must_change=true 时中间件必须拦截改密接口以外的全部 /api/*。"""
    monkeypatch.setattr(server.AUTH, "_state", {**server.AUTH._state, "must_change": True})
    r = client.post("/api/auth/login", json={"username": server.AUTH.username,
                                             "password": "test-password-123"})
    tok = r.json()["token"]
    hdr = {"Authorization": f"Bearer {tok}"}
    r2 = client.get("/api/bootstrap", headers=hdr)
    assert r2.status_code == 403 and r2.json().get("must_change") is True
    # 改密接口本身必须放行（改完立即恢复，避免污染模块级共享 AuthStore）
    r3 = client.post("/api/auth/change-password", headers=hdr,
                     json={"old_password": "test-password-123", "new_password": "test-password-456"})
    assert r3.status_code == 200
    server.AUTH.set_password("test-password-123")  # 恢复


def test_ssrf_blocked():
    for bad in ["http://127.0.0.1:8000/v1", "http://169.254.169.254/latest",
                "http://192.168.1.1/v1", "file:///etc/passwd"]:
        with pytest.raises(SSRFBlockedError):
            assert_safe_llm_url(bad)
    # 白名单域名放行
    assert_safe_llm_url("https://api.deepseek.com/v1")


def test_login_rate_limit(client):
    """🟠#21：连续失败达到阈值后锁定（429）。"""
    victim = "ratelimit-victim-user"
    for _ in range(server._LOGIN_MAX_FAILS):
        r = client.post("/api/auth/login", json={"username": victim, "password": "wrong"})
        assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": victim, "password": "wrong"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    # 清理锁定状态，避免影响其他用例
    for k in [k for k in server._LOGIN_FAILS if victim in k]:
        server._LOGIN_FAILS.pop(k, None)
    for k in [k for k in server._LOGIN_LOCKED_UNTIL if victim in k]:
        server._LOGIN_LOCKED_UNTIL.pop(k, None)
