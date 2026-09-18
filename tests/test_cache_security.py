# -*- coding: utf-8 -*-
"""cache_security.py 安全测试（此前零覆盖）

覆盖：正常读写回环、篡改载荷拒绝、密钥更换后旧缓存失效、
受限反序列化白名单、磁盘 LRU 淘汰。
"""
import os

import numpy as np
import pytest

from src.utils import cache_security as cs


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    d = str(tmp_path / "cache")
    # 密钥放独立文件（与缓存目录分离，口径）
    key_path = str(tmp_path / "cfg" / ".cache_secret")
    monkeypatch.setenv("ENERGY_CACHE_SECRET_FILE", key_path)
    cs._KEY_CACHE = None  # 强制重新加载密钥
    yield d
    cs._KEY_CACHE = None


def test_roundtrip(cache_dir):
    obj = {"a": 1, "arr": np.arange(5)}
    assert cs.save_signed(cache_dir, "k1.pkl", obj) is True
    back = cs.load_signed(cache_dir, "k1.pkl")
    assert back["a"] == 1 and np.array_equal(back["arr"], np.arange(5))


def test_tampered_payload_rejected(cache_dir):
    cs.save_signed(cache_dir, "k2.pkl", {"v": 1})
    path = os.path.join(cache_dir, "k2.pkl")
    raw = open(path, "rb").read()
    # 翻转载荷一个字节
    head, sig, payload = raw.split(b"\n", 2)
    bad_payload = b"X" + payload[1:]
    with open(path, "wb") as f:
        f.write(head + b"\n" + sig + b"\n" + bad_payload)
    assert cs.load_signed(cache_dir, "k2.pkl") is None


def test_forged_signature_rejected(cache_dir):
    cs.save_signed(cache_dir, "k3.pkl", {"v": 1})
    path = os.path.join(cache_dir, "k3.pkl")
    raw = open(path, "rb").read()
    head, sig, payload = raw.split(b"\n", 2)
    forged_sig = ("f" * 64).encode()
    with open(path, "wb") as f:
        f.write(head + b"\n" + forged_sig + b"\n" + payload)
    assert cs.load_signed(cache_dir, "k3.pkl") is None


def test_key_rotation_invalidates_cache(cache_dir, tmp_path, monkeypatch):
    cs.save_signed(cache_dir, "k4.pkl", {"v": 1})
    assert cs.load_signed(cache_dir, "k4.pkl") is not None
    # 换一个密钥文件 → 旧缓存全部失效（安全优先于命中）
    monkeypatch.setenv("ENERGY_CACHE_SECRET_FILE", str(tmp_path / "cfg2" / ".cache_secret"))
    cs._KEY_CACHE = None
    assert cs.load_signed(cache_dir, "k4.pkl") is None


def test_restricted_unpickler_blocks_arbitrary_classes(cache_dir):
    """白名单外的类引用（如 subprocess.Popen）不得被反序列化。"""
    import pickle as _pickle
    import hashlib, hmac as _hmac
    import subprocess as _subprocess
    payload = _pickle.dumps({"evil": _subprocess.Popen})  # 类引用 → find_class("subprocess", "Popen")
    key = cs._secret_key(cache_dir)
    sig = _hmac.new(key, payload, hashlib.sha256).hexdigest()
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, "evil.pkl"), "wb") as f:
        f.write(cs._MAGIC + b"\n" + sig.encode() + b"\n" + payload)
    try:
        out = cs.load_signed(cache_dir, "evil.pkl")
    except Exception:
        pass  # 抛异常也接受（拒绝即可）
    else:
        # 若未抛异常，返回值也不得携带白名单外的对象
        ok = out is None or not isinstance(getattr(out, "get", lambda k: None)("evil"), type)
        assert ok, "白名单外的类被成功反序列化"


def test_disk_lru_eviction(cache_dir):
    for i in range(8):
        cs.save_signed(cache_dir, f"lru_{i}.pkl", {"i": i})
        if i < 4:
            time_sleep_stub = None  # 前 4 个刻意变"旧"
    import time as _time
    # 把前 4 个 mtime 改旧
    for i in range(4):
        p = os.path.join(cache_dir, f"lru_{i}.pkl")
        old = _time.time() - 10_000
        os.utime(p, (old, old))
    removed = cs.enforce_disk_lru(cache_dir, max_files=6, max_total_mb=1024)
    assert removed == 2, f"LRU 应淘汰最旧的 2 个，实际淘汰 {removed}"
    assert not os.path.exists(os.path.join(cache_dir, "lru_0.pkl"))
    assert os.path.exists(os.path.join(cache_dir, "lru_7.pkl"))
