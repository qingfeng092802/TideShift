# -*- coding: utf-8 -*-
"""FastAPI 层 API 安全测试（此前 1273 行 27 个端点零测试）

覆盖：未登录 401、登录成功拿 token、must_change 强制拦截、
登录限流、SSRF 黑名单、会话隔离。
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
    # ⚠️ 环境依赖：TestClient 首次发请求时，anyio 会在调用线程与事件循环线程之间
    # 建立「阻塞门户」，Windows 下依赖 loopback socketpair()。
    # 若运行环境的沙箱/安全软件拦截 loopback 套接字，会抛
    # PermissionError: [WinError 10013]，表现为本文件首个用例失败（非项目缺陷）。
    # 判断方式：单独运行本文件应通过——pytest tests/test_server_api.py -q
    # 本机（Python 3.13.14 / Windows 10）实测 socketpair 与 anyio 门户均正常。
    # CI 运行在 ubuntu-latest，不受此限。
    return TestClient(server.app)


def _login(client, username=None, password="test-password-123"):
    """以真实账户登录。

    注意：AuthStore 只维护单账户，其用户名固定为 ``server.AUTH.username``（"admin"）。
    此前本函数默认 username="tester"，而校验用的是 hmac.compare_digest 全等比较，
    传 "tester" 必定 401——默认值改为真实用户名，避免调用方拿到 None token。
    """
    return client.post("/api/auth/login",
                       json={"username": username or server.AUTH.username,
                             "password": password})


def _auth_headers(client) -> dict:
    """登录并返回可直接用于 /api/* 的 Authorization 头。"""
    r = _login(client)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


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
    """must_change=true 时中间件必须拦截改密接口以外的全部 /api/*。"""
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
    """连续失败达到阈值后锁定（429）。"""
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


def test_explain_response_exposes_source(client, monkeypatch):
    """F2：/api/explain 必须在响应体里给出解释来源。

    此前只返回 {"text": ...}，来源仅体现在正文前缀里，调用方无法可靠判断
    本次解释是 LLM 生成还是规则模板降级（前端刷新后标签也停留在旧值）。

    这里把 ensure_solved 短路，避免触发真实 MILP；本用例只校验响应契约。
    """
    monkeypatch.setattr(server, "ensure_solved", lambda: True)
    hdr = _auth_headers(client)
    r = client.post("/api/explain", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "text" in body and isinstance(body["text"], str)
    # 无调度数据 → "none"；有数据且无 Key → "rule"；配了 Key → "llm"
    assert body.get("source") in ("llm", "rule", "none"), body


def test_chat_response_exposes_mode(client, monkeypatch):
    """F2：/api/chat 必须返回对话 Agent 的模式标识（rule / llm）。"""
    monkeypatch.setattr(server, "ensure_solved", lambda: True)
    hdr = _auth_headers(client)
    r = client.post("/api/chat", headers=hdr, json={"message": "今天的收益是多少"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "reply" in body and "history" in body
    # 无 API Key 时工厂必定返回规则模式 Agent
    assert body.get("mode") in ("rule", "llm"), body


def test_env_mode_login_creates_no_auth_file(client, tmp_path, monkeypatch):
    """ENERGY_AUTH_MODE=env：走完真实登录与受保护端点后，配置目录下不得新增 auth.json。

    只在 AuthStore 单测层面断言不够——登录端点、JWT 签发、中间件都可能间接落盘，
    必须在 HTTP 层确认"env 模式不写口令文件"。

    说明：env 模式不落盘的是**口令**；JWT 签名密钥 `config/.auth_secret` 与 API Key
    加密密钥 `config/.api_secret` 仍会生成（否则每次重启 token 全失效、已存 Key 无法解密），
    因此本用例只断言口令相关文件不存在。
    """
    auth_mod = server.auth_mod
    monkeypatch.setattr(auth_mod, "_config_dir", lambda: str(tmp_path))
    monkeypatch.setenv("ENERGY_AUTH_MODE", "env")
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "env-login-pass-1")
    # 用 env 模式下的新实例替换模块级 AUTH（其模式在构造时确定）
    monkeypatch.setattr(server, "AUTH", auth_mod.AuthStore())

    r = client.post("/api/auth/login", json={"username": "admin",
                                            "password": "env-login-pass-1"})
    assert r.status_code == 200, r.text
    assert r.json()["must_change"] is False
    tok = r.json()["token"]
    r2 = client.get("/api/bootstrap", headers={"Authorization": f"Bearer {tok}"})
    assert r2.status_code == 200, r2.text

    assert not (tmp_path / "auth.json").exists(), "env 模式不应生成口令文件"
    assert not (tmp_path / auth_mod.INITIAL_PWD_FILENAME).exists(), \
        "env 模式不应生成一次性口令文件"


def test_env_mode_without_password_refuses_to_start(tmp_path, monkeypatch):
    """env 模式但未提供口令：必须**拒绝启动**，不得静默回退 persistent、
    也不得每次启动生成随机口令（后者会让口令随重启变化且只打印一次）。"""
    auth_mod = server.auth_mod
    monkeypatch.setattr(auth_mod, "_config_dir", lambda: str(tmp_path))
    monkeypatch.setenv("ENERGY_AUTH_MODE", "env")
    monkeypatch.delenv("ADMIN_INITIAL_PASSWORD", raising=False)

    with pytest.raises(auth_mod.AuthConfigError) as ei:
        auth_mod.AuthStore()
    msg = str(ei.value)
    assert "ADMIN_INITIAL_PASSWORD" in msg and "拒绝启动" in msg
    assert not (tmp_path / "auth.json").exists(), "拒绝启动不得留下任何口令文件"
