# -*- coding: utf-8 -*-
"""FastAPI 层 API 安全测试（这一层曾经整层零测试）

覆盖：未登录 401、登录成功拿 token、must_change 强制拦截、
登录限流、SSRF 黑名单、会话隔离、SSE 端点的追溯口径。
不触发 MILP 求解——全部是秒级安全语义测试。
"""
import os
import asyncio
import time
import sys

import pytest
from fastapi.testclient import TestClient

# server.py 以脚本目录方式导入 auth（import auth），需先把 backend 目录注入 sys.path
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

import server  # noqa: E402  backend/server.py
from src.utils import trace  # noqa: E402
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


def _sse_events(body: str) -> list:
    """把 SSE 文本解成 data 事件列表（注释行/心跳之外的非 data 块忽略）。"""
    import json
    out = []
    for block in body.split("\n\n"):
        block = block.strip()
        if block.startswith("data:"):
            out.append(json.loads(block[5:].strip()))
    return out


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


def test_chat_stream_run_is_traced_from_inside(client, monkeypatch):
    """SSE 端点的追溯 run 必须开在生成器**内部**。

    用 `@trace.traced` 包端点时，`with` 只圈到"生成器对象被创建"就退出，
    真正消费发生在之后 —— 记出来的运行是亚毫秒、属性全空，追溯页拿到一个
    看起来正常其实什么都没量的数字。所以这里断言的是"属性齐全"，
    它只有在生成器内部开 run 才可能成立。
    """
    monkeypatch.setattr(server, "ensure_solved", lambda: True)
    trace.reset()
    hdr = _auth_headers(client)
    msg = "今天的收益是多少"
    r = client.post("/api/chat/stream", headers=hdr, json={"message": msg})
    assert r.status_code == 200, r.text
    assert '"done"' in r.text
    runs = [x for x in trace.recent(limit=50) if x["kind"] == "chat_stream"]
    assert runs, "流式对话没留下任何运行记录"
    last = runs[-1]
    assert last["status"] == "ok"
    assert last["attrs"]["question_len"] == len(msg)
    assert last["attrs"]["mode"] in ("rule", "llm")
    assert "stream" in last["attrs"]


def test_chat_stream_carries_notice_and_closes_turn(client, monkeypatch):
    """降级原因要作为 SSE 事件送达，且回合必须闭合。

    只写日志等于没写：界面上看到的只是"回复很机械"，排查的人会去猜是不是流式坏了。
    断言走 `done` 事件里的 history 而不是 `server._sess()`——请求按 JWT sub 取会话，
    测试函数里没有请求上下文，`_sess()` 拿到的是另一个默认实例，直接读它必然对不上。
    """
    monkeypatch.setattr(server, "ensure_solved", lambda: True)
    hdr = _auth_headers(client)
    r = client.post("/api/chat/stream", headers=hdr, json={"message": "今天收益多少"})
    assert r.status_code == 200, r.text
    assert '"notice"' in r.text, "规则模式没有把降级原因发出来"
    done = [e for e in _sse_events(r.text) if e.get("done")]
    assert done, r.text
    roles = [m["role"] for m in done[0]["history"][-2:]]
    assert roles == ["user", "assistant"], done[0]["history"][-2:]
    # notice 是给界面看的状态，不是助手说的话：进了 history 就会被当对话上下文回灌
    notice = [e["notice"] for e in _sse_events(r.text) if "notice" in e][0]
    assert notice and notice not in done[0]["history"][-1]["content"], notice


def test_chat_stream_emits_heartbeat_while_silent(client, monkeypatch):
    """工具/模型静默超过阈值时必须发心跳。

    实测口径：追溯日志里 solve 单次 p50 49.7 s、max 68.2 s，这段时间 SSE 上
    一个字节都没有——页面表现为"卡在空气泡"，任何中间层也可能按空闲超时掐连接。
    """
    monkeypatch.setattr(server, "ensure_solved", lambda: True)
    monkeypatch.setattr(server, "_SSE_IDLE_S", 0.3)
    monkeypatch.setattr(server.AppState, "active_llm",
                        lambda self: ("sk-x", "https://api.deepseek.com/v1",
                                      "deepseek-chat", {"stream": True}))

    class SlowAgent:
        mode = "llm"
        fallback_reason = ""

        async def astream(self, _msg):
            await asyncio.sleep(1.0)          # 模拟一次工具求解
            yield "content", "好了"

    monkeypatch.setattr(server, "create_agent", lambda *a, **k: SlowAgent())
    hdr = _auth_headers(client)
    r = client.post("/api/chat/stream", headers=hdr, json={"message": "重跑一遍"})
    assert r.status_code == 200, r.text
    assert '"heartbeat"' in r.text, r.text
    assert '"delta": "好了"' in r.text or '"delta":"好了"' in r.text, r.text
    # 心跳/降级说明只是 UI 状态，一个字都不许进正文与 history：
    # 进了 history 就等于把"⏳ 已等待 4 s"当成助手说过的话回灌给下一轮模型。
    done = [e for e in _sse_events(r.text) if e.get("done")]
    assert done, r.text
    last = done[0]["history"][-1]["content"]
    assert last == "好了", repr(last)
    assert "heartbeat" not in last and "已等待" not in last


def test_chat_stream_reasoning_is_a_separate_event(client, monkeypatch):
    """思维链走独立事件，且不混进正文/history。

    reasoning 是模型的思考过程：进 acc 会被当正文渲染、还会永久留在 history 里，
    下一轮当成"助手说过的话"喂回模型。
    """
    monkeypatch.setattr(server, "ensure_solved", lambda: True)
    monkeypatch.setattr(server.AppState, "active_llm",
                        lambda self: ("sk-x", "https://api.deepseek.com/v1",
                                      "deepseek-chat", {"stream": True}))

    class ThinkAgent:
        mode = "llm"
        fallback_reason = ""

        async def astream(self, _msg):
            yield "reasoning", "先看价格档"
            yield "content", "净收益 1567.82 元"

    monkeypatch.setattr(server, "create_agent", lambda *a, **k: ThinkAgent())
    hdr = _auth_headers(client)
    r = client.post("/api/chat/stream", headers=hdr, json={"message": "为什么"})
    assert r.status_code == 200, r.text
    assert '"reasoning": "先看价格档"' in r.text, r.text
    done = [e for e in _sse_events(r.text) if e.get("done")]
    tail = done[0]["history"][-1]["content"]
    assert "先看价格档" not in tail, "思维链漏进了 history"
    assert "1567.82" in tail


def test_chat_stream_rule_mode_tool_does_not_freeze_loop(client, monkeypatch):
    """非流式（规则模式）的同步 `respond` 必须换线程跑。

    这条守的不是"有没有心跳"，而是**整个服务会不会被冻住**：`chat_stream` 是
    `async def`，原地调用一次 50 s 的 MILP 重跑，冻住的是事件循环——看板所有接口、
    进度轮询一起停摆，心跳也发不出去。断言方式：求解期间从另一个线程打 /api/health，
    能按时返回就说明循环还活着。
    """
    import threading

    monkeypatch.setattr(server, "ensure_solved", lambda: True)
    monkeypatch.setattr(server, "_SSE_IDLE_S", 0.3)
    monkeypatch.setattr(server.AppState, "active_llm",
                        lambda self: ("", "", "deepseek-chat", {"stream": False}))

    class SlowRuleAgent:
        mode = "rule"
        fallback_reason = "未配置 API Key，当前为规则模式"

        def respond(self, _msg):
            time.sleep(1.2)                     # 模拟一次工具里的真实求解
            return "已重跑"

    monkeypatch.setattr(server, "create_agent", lambda *a, **k: SlowRuleAgent())
    hdr = _auth_headers(client)
    lat = {}

    def poll():
        t0 = time.time()
        client.get("/api/progress", headers=hdr)
        lat["s"] = time.time() - t0

    th = threading.Thread(target=poll)
    th.start()
    time.sleep(0.35)                            # 让探测请求落在求解进行中
    r = client.post("/api/chat/stream", headers=hdr, json={"message": "重跑一遍"})
    th.join()
    assert r.status_code == 200, r.text
    assert '"heartbeat"' in r.text, r.text
    assert lat["s"] < 1.0, f"求解期间并发请求被拖到 {lat['s']:.2f}s → 事件循环被同步调用冻住"


def test_env_mode_never_forces_password_change(tmp_path, monkeypatch):
    """env 模式下 `must_change` 恒为 False —— 口令由部署者的环境变量托管，没有"初始口令"要轮换。

    这条本来不用测：代码就是这样写的。之所以钉住，是因为白屏事故里有人提议
    "把 env 模式的 must_change 改成 False"作为修复——而真正的死锁在前端：
    403 只抛错、不弹改密层，boot() 一崩就没界面。留着这条是为了下次别再去找
    一个不存在的原因。
    """
    auth_mod = server.auth_mod
    monkeypatch.setenv("ENERGY_AUTH_MODE", "env")
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "unit-test-env-pwd")
    monkeypatch.setattr(auth_mod, "_config_dir", lambda: str(tmp_path))
    store = auth_mod.AuthStore()
    assert store.must_change() is False
    assert store.verify("admin", "unit-test-env-pwd")
    # env 模式不读口令文件：即便旁边躺着一份 must_change=true 的文件也不算数
    assert not (tmp_path / "auth.json").exists()


def test_must_change_403_body_is_machine_readable(client, monkeypatch):
    """403 响应体必须带 `must_change: true`——前端靠它弹改密层，不能靠抠 detail 文案。

    同时确认改密接口本身在这期间是通的：`_MUST_CHANGE_ALLOW` 一旦漏配，
    用户就彻底没有出路（这正是白屏事故的形状）。
    """
    monkeypatch.setattr(server.AUTH, "must_change", lambda: True)
    hdr = _auth_headers(client)
    r = client.get("/api/bootstrap", headers=hdr)
    assert r.status_code == 403, r.text
    assert r.json().get("must_change") is True, r.json()
    cp = client.post("/api/auth/change-password", headers=hdr,
                     json={"old_password": "definitely-wrong", "new_password": "another-test-pwd"})
    assert cp.status_code == 400, cp.text        # 400=原口令错误（放行到了端点），不是 403
    assert "原口令" in cp.json()["detail"]


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
