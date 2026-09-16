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


def test_auth_config_dir_isolated_from_repo_tree(isolated_state_dir):
    """回归：pytest 不得把凭据写进仓库工作树。

    缺陷复现路径：`server.py` 在**导入时**就实例化 AuthStore，其 `_config_dir()`
    默认指向 `<项目根>/config/`，于是跑一次 pytest 就会在工作树里留下
    `config/auth.json` 与 `config/.auth_secret`。用户按 README 的
    「装依赖 → 跑 pytest → 启动服务」操作时，服务读到测试生成的随机口令而跳过
    `_create_default()`，`ADMIN_INITIAL_PASSWORD` 被**静默忽略**，任何口令都登录失败
    （阻断级，且极难自查）。

    conftest.py 通过 `ENERGY_CONFIG_DIR` 把认证/密钥落盘目录重定向到会话级临时目录，
    本用例锁定该不变量。同源问题也适用于 `ENERGY_CACHE_SECRET_FILE`。
    """
    import os

    resolved = os.path.abspath(auth_mod._config_dir())
    project_config = os.path.abspath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"))

    assert resolved == os.path.abspath(isolated_state_dir), (
        f"认证配置目录解析为 {resolved}，未指向测试临时目录 {isolated_state_dir}")
    assert resolved != project_config, \
        "认证配置目录仍指向仓库 config/——测试会污染工作树并导致用户启动后无法登录"
    assert os.path.abspath(os.environ["ENERGY_CONFIG_DIR"]) == resolved, \
        "ENERGY_CONFIG_DIR 与实际解析结果不一致"


# ---------- 认证模式（ENERGY_AUTH_MODE）与首启口令可用性 ----------

def test_auth_mode_parsing(monkeypatch):
    """模式解析：未设/空串/非法值一律回退 persistent（不因配置写错拒绝启动）。"""
    monkeypatch.delenv("ENERGY_AUTH_MODE", raising=False)
    assert auth_mod.auth_mode() == "persistent"
    monkeypatch.setenv("ENERGY_AUTH_MODE", "   ")
    assert auth_mod.auth_mode() == "persistent"
    monkeypatch.setenv("ENERGY_AUTH_MODE", "env")
    assert auth_mod.auth_mode() == "env"
    monkeypatch.setenv("ENERGY_AUTH_MODE", "never")
    assert auth_mod.auth_mode() == "persistent"


def test_env_mode_does_not_touch_disk(tmp_path, monkeypatch):
    """env 模式：口令只来自环境变量，不读也不写任何口令文件。"""
    monkeypatch.setattr(auth_mod, "_config_dir", lambda: str(tmp_path))
    monkeypatch.setenv("ENERGY_AUTH_MODE", "env")
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "env-managed-pass-1")

    st = auth_mod.AuthStore()
    assert st.password_managed_by_env is True
    assert st.must_change() is False
    assert st.verify("admin", "env-managed-pass-1") is True
    assert st.verify("admin", "wrong") is False
    assert st.verify("root", "env-managed-pass-1") is False
    assert not (tmp_path / "auth.json").exists(), "env 模式不应落盘口令文件"
    with pytest.raises(auth_mod.AuthModeError):
        st.set_password("whatever-12345")


def test_persistent_random_boot_writes_one_time_password_file(tmp_path, monkeypatch):
    """首启随机口令要落一份 0600 的一次性副本，改密成功后自动删除。

    原实现只在控制台打印一次：容器日志被刷掉或用户漏看就再也拿不到口令。
    """
    monkeypatch.setattr(auth_mod, "_config_dir", lambda: str(tmp_path))
    monkeypatch.setenv("ENERGY_AUTH_MODE", "persistent")
    monkeypatch.delenv("ADMIN_INITIAL_PASSWORD", raising=False)

    st = auth_mod.AuthStore()
    f = tmp_path / auth_mod.INITIAL_PWD_FILENAME
    assert f.exists(), "首启随机口令应写出一次性口令文件"
    lines = [l for l in f.read_text(encoding="utf-8").splitlines() if l.startswith("初始口令：")]
    assert lines, "一次性口令文件应包含口令行"
    pwd = lines[0].split("：", 1)[1].strip()
    assert len(pwd) >= 12
    assert st.verify("admin", pwd) is True
    assert st.must_change() is True

    st.set_password("Brand-New-Pass-1")
    assert not f.exists(), "改密成功后一次性口令文件应被删除"
    assert st.verify("admin", "Brand-New-Pass-1") is True
    assert st.verify("admin", pwd) is False


def test_env_supplied_initial_password_writes_no_one_time_file(tmp_path, monkeypatch):
    """环境变量指定口令时不应生成一次性口令文件，也不必强制改密。"""
    monkeypatch.setattr(auth_mod, "_config_dir", lambda: str(tmp_path))
    monkeypatch.setenv("ENERGY_AUTH_MODE", "persistent")
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "from-env-pass-1234")

    st = auth_mod.AuthStore()
    assert not (tmp_path / auth_mod.INITIAL_PWD_FILENAME).exists()
    assert st.must_change() is False
    assert st.verify("admin", "from-env-pass-1234") is True


def test_authstore_auto_create_false_does_not_mint_password(tmp_path, monkeypatch):
    """auto_create=False：口令文件缺失时不顺手造一份随机口令。

    运维命令（重置口令）依赖该行为——否则控制台会先后出现两个口令，使用者无从分辨。
    """
    monkeypatch.setattr(auth_mod, "_config_dir", lambda: str(tmp_path))
    monkeypatch.setenv("ENERGY_AUTH_MODE", "persistent")
    monkeypatch.delenv("ADMIN_INITIAL_PASSWORD", raising=False)

    st = auth_mod.AuthStore(auto_create=False)
    assert not (tmp_path / "auth.json").exists(), "auto_create=False 不应落盘"
    assert not (tmp_path / auth_mod.INITIAL_PWD_FILENAME).exists()
    st.set_password("Explicit-Set-1234")
    assert (tmp_path / "auth.json").exists()
    assert st.verify("admin", "Explicit-Set-1234") is True


def test_stale_initial_password_file_cleaned_when_must_change_false(tmp_path, monkeypatch):
    """must_change=false 时启动应清掉残留的一次性口令文件。

    一次性口令文件只在"初始口令仍然有效"期间才该存在。崩溃中断、手工改密、
    或直接替换 `auth.json` 都可能把它留下——那就等于磁盘上长期躺着一份可读的初始口令。
    因此每次启动都按 must_change 状态纠正。
    """
    monkeypatch.setattr(auth_mod, "_config_dir", lambda: str(tmp_path))
    monkeypatch.setenv("ENERGY_AUTH_MODE", "persistent")
    monkeypatch.delenv("ADMIN_INITIAL_PASSWORD", raising=False)

    auth_mod.AuthStore()                       # 首启：生成随机口令 + 一次性文件
    f = tmp_path / auth_mod.INITIAL_PWD_FILENAME
    assert f.exists(), "前置条件：首启应写出一次性口令文件"

    # 模拟"手工改密 / 直接替换 auth.json"：文件里 must_change 已为 false，但密码文件残留
    d = json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))
    d["must_change"] = False
    (tmp_path / "auth.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    assert f.exists(), "前置条件：残留文件仍在"

    auth_mod.AuthStore()                       # 再次启动
    assert not f.exists(), "must_change=false 时启动应清理残留的一次性口令文件"


def test_initial_password_file_kept_while_must_change_true(tmp_path, monkeypatch):
    """反向约束：must_change=true 期间不得误删——否则用户还没登录就丢了唯一口令来源。"""
    monkeypatch.setattr(auth_mod, "_config_dir", lambda: str(tmp_path))
    monkeypatch.setenv("ENERGY_AUTH_MODE", "persistent")
    monkeypatch.delenv("ADMIN_INITIAL_PASSWORD", raising=False)

    auth_mod.AuthStore()
    f = tmp_path / auth_mod.INITIAL_PWD_FILENAME
    assert f.exists()

    auth_mod.AuthStore()                       # 重启（此时尚未改密）
    assert f.exists(), "must_change=true 期间一次性口令文件必须保留"


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
