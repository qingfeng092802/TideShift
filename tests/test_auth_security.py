# -*- coding: utf-8 -*-
"""🔴#7/T1：backend/auth.py 安全测试（此前零覆盖）

覆盖：JWT 篡改/过期/算法混淆拒绝、错误口令、中文用户名（compare_digest 非 ASCII）、
初始口令不再使用 admin/admin123、must_change 语义。
"""
import base64
import json
import time

import pytest

from backend import auth as auth_mod


def test_jwt_valid_roundtrip():
    token = auth_mod.jwt_encode({"sub": "tester"})
    payload = auth_mod.jwt_verify(token)
    assert payload is not None and payload["sub"] == "tester"


def test_jwt_tampered_payload_rejected():
    """篡改 payload（改 sub/改 exp）必须验签失败。"""
    token = auth_mod.jwt_encode({"sub": "tester"})
    h, p, s = token.split(".")
    body = json.loads(auth_mod._b64u_d(p))
    body["sub"] = "attacker"
    forged = f"{h}.{auth_mod._b64u(json.dumps(body).encode())}.{s}"
    assert auth_mod.jwt_verify(forged) is None


def test_jwt_alg_confusion_rejected():
    """alg=none / alg=HS512 混淆攻击必须拒绝（P0-02 固定 HS256）。"""
    token = auth_mod.jwt_encode({"sub": "tester"})
    h, p, s = token.split(".")
    header_none = auth_mod._b64u(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    assert auth_mod.jwt_verify(f"{header_none}.{p}.") is None
    header_512 = auth_mod._b64u(json.dumps({"alg": "HS512", "typ": "JWT"}).encode())
    assert auth_mod.jwt_verify(f"{header_512}.{p}.{s}") is None


def test_jwt_expired_rejected():
    token = auth_mod.jwt_encode({"sub": "tester"}, ttl_s=-1)
    assert auth_mod.jwt_verify(token) is None


def test_verify_wrong_password():
    store = auth_mod.AuthStore()
    assert store.verify(store.username, "definitely-wrong-password") is False


def test_verify_non_ascii_username_no_crash():
    """🔴#22：中文用户名此前使 compare_digest 抛 TypeError → 500；必须返回 False。"""
    store = auth_mod.AuthStore()
    try:
        ok = store.verify("管理员", "whatever")
    except TypeError:
        pytest.fail("中文用户名触发 compare_digest TypeError（🔴#22 未修复）")
    assert ok is False


def test_default_initial_password_not_admin123(monkeypatch, tmp_path):
    """🔴#3：首启默认口令不得是 admin/admin123（除非显式经环境变量注入）。"""
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "")
    # 在临时目录重建 AuthStore（避免污染真实 config/auth.json）
    monkeypatch.setattr(auth_mod, "_config_dir", lambda: str(tmp_path))
    store = auth_mod.AuthStore()
    assert store.verify("admin", "admin123") is False, "默认口令 admin123 仍可用（🔴#3 未修复）"
    assert store.must_change() is True


def test_change_password_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_mod, "_config_dir", lambda: str(tmp_path))
    store = auth_mod.AuthStore()
    store.set_password("a-strong-new-pass-1")
    assert store.verify(store.username, "a-strong-new-pass-1") is True
    assert store.must_change() is False
