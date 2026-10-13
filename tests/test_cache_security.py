# -*- coding: utf-8 -*-
"""cache_security.py 安全测试（此前零覆盖）

覆盖：正常读写回环、dataclass/ndarray/DatetimeIndex 保真往返、篡改载荷拒绝、
伪造签名拒绝、密钥更换后旧缓存失效、**纯数据反序列化的白名单边界**、
旧版 pickle 缓存判为未命中、体积上限、磁盘 LRU 淘汰、**文件名路径穿越**。

本文件是 P1.1（反序列化 RCE）修复的回归防线：原实现用
`_RestrictedUnpickler` 做**模块名前缀**白名单，`src.utils.logger` 这类前缀一旦命中就
`return super().find_class(module, name)`，于是点号名 `src.utils.logger` + `os.system`
可以穿透并**执行任意函数**。改用 JSON 后不再存在任何"调用任意对象"的原语，
下面 `test_json_type_tag_outside_whitelist_*` 三例把这条边界钉死。

本文件同时是 P1.5（solve 缓存文件名路径穿越，审查报告 H-5）的回归防线：
文件名与 HMAC 签名一样是**不可信输入**，见文件末尾「路径穿越」一节。
"""
import dataclasses
import hashlib
import hmac
import json
import os
import time

import numpy as np
import pandas as pd
import pytest

from src.agents.coordinator_agent import DailyReport
from src.agents.demand_response_agent import DRResponse, DRSignal
from src.agents.storage_optimization_agent import ScheduleResult
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


def _write_signed_raw(cache_dir, filename, payload: bytes):
    """按模块格式手工写一个"签名正确但内容可疑"的载荷，用于测试解码侧边界。"""
    key = cs._secret_key(cache_dir)
    sig = hmac.new(key, payload, hashlib.sha256).hexdigest()
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, filename), "wb") as f:
        f.write(cs._MAGIC + b"\n" + sig.encode("ascii") + b"\n" + payload)


def test_roundtrip(cache_dir):
    obj = {"a": 1, "arr": np.arange(5)}
    assert cs.save_signed(cache_dir, "k1" + cs.CACHE_SUFFIX, obj) is True
    back = cs.load_signed(cache_dir, "k1" + cs.CACHE_SUFFIX)
    assert back["a"] == 1 and np.array_equal(back["arr"], np.arange(5))


def test_roundtrip_preserves_dtypes_and_shapes(cache_dir):
    """ndarray 的 dtype/形状必须原样往返（缓存里的价格/功率序列靠它保真）。"""
    obj = {
        "f": np.linspace(0, 1, 7, dtype=np.float32),
        "i": np.arange(6, dtype=np.int64),
        "b": np.array([True, False]),
        "m": np.arange(6, dtype=np.float64).reshape(2, 3),
        "scalar": np.float64(1.5),
        "none": None,
        "tuple": (1, 2.5, "x"),
        "map": {1: "one", 2: "two"},      # 非字符串键 → __ts_map__
    }
    assert cs.save_signed(cache_dir, "k2" + cs.CACHE_SUFFIX, obj) is True
    back = cs.load_signed(cache_dir, "k2" + cs.CACHE_SUFFIX)
    assert back["f"].dtype == np.float32 and np.allclose(back["f"], obj["f"])
    assert back["i"].dtype == np.int64 and np.array_equal(back["i"], obj["i"])
    assert back["b"].dtype == np.bool_ and np.array_equal(back["b"], obj["b"])
    assert back["m"].shape == (2, 3) and np.allclose(back["m"], obj["m"])
    assert back["scalar"] == pytest.approx(1.5)
    assert back["none"] is None
    assert back["tuple"] == (1, 2.5, "x")
    assert back["map"] == {1: "one", 2: "two"}


def test_roundtrip_datetimeindex(cache_dir):
    idx = pd.date_range("2024-07-15", periods=4, freq="15min")
    assert cs.save_signed(cache_dir, "k3" + cs.CACHE_SUFFIX, {"time_index": idx}) is True
    back = cs.load_signed(cache_dir, "k3" + cs.CACHE_SUFFIX)["time_index"]
    assert isinstance(back, pd.DatetimeIndex) and back.equals(idx)


def test_roundtrip_allowed_dataclasses(cache_dir):
    """白名单内的 4 个数据载体必须逐字段保真——它们是缓存载荷的全部内容。"""
    report = DailyReport(
        date="2024-07-30", arbitrage_revenue_yuan=1816.2659112058213,
        dr_subsidy_yuan=159.84, degradation_cost_yuan=388.3696490442338,
        net_revenue_yuan=1587.7362621615875, charge_energy_kwh=2505.713,
        discharge_energy_kwh=2261.406, max_battery_temp_c=46.915252206565214,
        equivalent_cycles=1.456386183915877, forecast_mape=3.12,
        dr_events_count=2, alerts=["负荷预测降级"], explanation="测试", explanation_source="rule")
    sched = ScheduleResult(
        charge_power_kw=np.zeros(96), discharge_power_kw=np.zeros(96),
        net_power_kw=np.zeros(96), soc=np.full(97, 0.5), battery_temp_c=np.full(97, 30.0),
        arbitrage_revenue_yuan=1.0, degradation_cost_yuan=2.0, net_revenue_yuan=-1.0,
        charge_energy_kwh=0.0, discharge_energy_kwh=0.0, max_battery_temp_c=30.0,
        equivalent_cycles=0.0, solver_status="Optimal", terminal_soc=0.5, mip_gap_pct=1.0)
    sig = DRSignal(start_time="15:00", end_time="17:00",
                   target_reduction_kw=400.0, subsidy_per_kwh=0.8)
    resp = DRResponse(
        accepted=True, reason="响应成功", adjusted_power_kw=np.zeros(96),
        response_power_kw=108.0, response_energy_kwh=216.0, subsidy_revenue_yuan=172.83,
        degradation_cost_yuan=5.0, net_dr_revenue_yuan=167.83, max_temp_during_dr_c=43.1,
        feasibility_check={"thermal_safety": {"safe": True, "max_safe_power": 320.0},
                           "revenue": {"net_revenue": 167.83}})
    obj = {"v": 2, "report": report, "baseline": sched, "dr": [sig], "viz": {"dr_responses": [resp]}}
    assert cs.save_signed(cache_dir, "k4" + cs.CACHE_SUFFIX, obj) is True
    back = cs.load_signed(cache_dir, "k4" + cs.CACHE_SUFFIX)

    assert isinstance(back["report"], DailyReport)
    assert back["report"] == report          # DailyReport 全是标量/字符串，可整体比较
    got_sched = back["baseline"]
    assert isinstance(got_sched, ScheduleResult)
    # ScheduleResult 带 ndarray，dataclass __eq__ 会落到 bool(array) 上，必须逐字段比
    for f in dataclasses.fields(ScheduleResult):
        a, b = getattr(got_sched, f.name), getattr(sched, f.name)
        if isinstance(b, np.ndarray):
            assert np.array_equal(a, b) and a.dtype == b.dtype, f.name
        else:
            assert a == b, f.name
    assert [type(x) for x in back["dr"]] == [DRSignal] and back["dr"][0] == sig
    got = back["viz"]["dr_responses"][0]
    assert isinstance(got, DRResponse)
    assert got.accepted is True and got.reason == "响应成功"
    assert np.array_equal(got.adjusted_power_kw, resp.adjusted_power_kw)
    assert got.subsidy_revenue_yuan == pytest.approx(172.83)
    assert got.feasibility_check == resp.feasibility_check


def test_unregistered_dataclass_cannot_be_encoded(cache_dir):
    """新增类型必须显式登记，否则写入侧直接失败（而不是悄悄退化成 pickle）。"""
    @dataclasses.dataclass
    class NotRegistered:
        x: int = 1

    assert cs.save_signed(cache_dir, "bad" + cs.CACHE_SUFFIX, NotRegistered()) is False
    assert cs.load_signed(cache_dir, "bad" + cs.CACHE_SUFFIX) is None


def test_unsupported_object_cannot_be_encoded(cache_dir):
    """任意对象（含"能执行东西"的对象）在写入侧就该被拒绝。"""
    assert cs.save_signed(cache_dir, "bad2" + cs.CACHE_SUFFIX, {"f": len}) is False
    assert cs.save_signed(cache_dir, "bad3" + cs.CACHE_SUFFIX, {"o": object()}) is False


def test_tampered_payload_rejected(cache_dir):
    cs.save_signed(cache_dir, "k5" + cs.CACHE_SUFFIX, {"v": 1})
    path = os.path.join(cache_dir, "k5" + cs.CACHE_SUFFIX)
    raw = open(path, "rb").read()
    # 翻转载荷一个字节
    head, sig, payload = raw.split(b"\n", 2)
    bad_payload = b"X" + payload[1:]
    with open(path, "wb") as f:
        f.write(head + b"\n" + sig + b"\n" + bad_payload)
    assert cs.load_signed(cache_dir, "k5" + cs.CACHE_SUFFIX) is None


def test_forged_signature_rejected(cache_dir):
    cs.save_signed(cache_dir, "k6" + cs.CACHE_SUFFIX, {"v": 1})
    path = os.path.join(cache_dir, "k6" + cs.CACHE_SUFFIX)
    raw = open(path, "rb").read()
    head, sig, payload = raw.split(b"\n", 2)
    forged_sig = ("f" * 64).encode()
    with open(path, "wb") as f:
        f.write(head + b"\n" + forged_sig + b"\n" + payload)
    assert cs.load_signed(cache_dir, "k6" + cs.CACHE_SUFFIX) is None


def test_key_rotation_invalidates_cache(cache_dir, tmp_path, monkeypatch):
    cs.save_signed(cache_dir, "k7" + cs.CACHE_SUFFIX, {"v": 1})
    assert cs.load_signed(cache_dir, "k7" + cs.CACHE_SUFFIX) is not None
    # 换一个密钥文件 → 旧缓存全部失效（安全优先于命中）
    monkeypatch.setenv("ENERGY_CACHE_SECRET_FILE", str(tmp_path / "cfg2" / ".cache_secret"))
    cs._KEY_CACHE = None
    assert cs.load_signed(cache_dir, "k7" + cs.CACHE_SUFFIX) is None


# ---------------------------------------------------------------------------
# P1.1 回归防线：签名正确也拿不到"执行任意东西"的能力
# ---------------------------------------------------------------------------

def test_json_type_tag_outside_whitelist_rejected(cache_dir, tmp_path):
    """白名单外的类名（含原漏洞的 `src.utils.logger` 前缀穿透形状）必须被拒绝。

    原实现：`_ALLOWED_MODULES` 含 `"src."`，于是 `find_class("src.utils.logger", "os.system")`
    前缀命中 → `super().find_class` 解析出**真的 os.system 并调用它**。
    新实现按精确 `(module, name)` 元组反查，任何非登记项直接抛 CacheDecodeError。
    """
    marker = tmp_path / "RCE_MARKER"
    evil_types = [
        "os.system",                        # 直接系统命令
        "subprocess.Popen",                 # 进程
        "src.utils.logger.os.system",       # 原漏洞的点号名穿透形状
        "src.agents.coordinator_agent.DailyReport.evil",   # 合法前缀 + 非法后缀
        "builtins.eval",
        "importlib.import_module",
    ]
    for i, t in enumerate(evil_types):
        payload = json.dumps(
            {"__ts_type__": t, "fields": {"command": f"echo pwned > {marker}"}},
            ensure_ascii=False).encode("utf-8")
        name = f"evil_{i}{cs.CACHE_SUFFIX}"
        _write_signed_raw(cache_dir, name, payload)
        assert cs.load_signed(cache_dir, name) is None, f"载荷被接受: {t}"
    # 关键断言：整轮下来没有任何东西被执行
    assert not marker.exists(), "白名单外的类型被执行了"


def test_json_unknown_fields_rejected(cache_dir):
    """字段名对不上当前代码（跨版本载荷）时按未命中处理，而不是 TypeError 外溢。"""
    payload = json.dumps({
        "__ts_type__": "src.agents.coordinator_agent.DailyReport",
        "fields": {"date": "2024-07-30", "no_such_field": 1},
    }).encode("utf-8")
    _write_signed_raw(cache_dir, "stale" + cs.CACHE_SUFFIX, payload)
    assert cs.load_signed(cache_dir, "stale" + cs.CACHE_SUFFIX) is None


def test_malformed_payload_returns_none(cache_dir):
    """非 JSON / 结构不对的载荷一律返回 None，不把异常抛给调用方。"""
    for i, raw in enumerate([b"not json at all", b"[1,2,3", b"null",
                             b'{"__ts_array__": {"dtype": "O", "shape": [1], "data": [1]}}',
                             b'{"__ts_array__": {"dtype": "float64"}}',      # 缺 shape/data
                             b'{"__ts_type__": "src.agents.coordinator_agent.DailyReport"}'  # 缺 fields
                             ]):
        name = f"bad_{i}{cs.CACHE_SUFFIX}"
        _write_signed_raw(cache_dir, name, raw)
        assert cs.load_signed(cache_dir, name) is None, raw


def test_json_native_scalar_payload_is_accepted(cache_dir):
    """JSON 原生标量属于"纯数据"，解码成功（不是漏洞面：它没有任何可执行语义）。"""
    _write_signed_raw(cache_dir, "s" + cs.CACHE_SUFFIX, b'"just a string"')
    assert cs.load_signed(cache_dir, "s" + cs.CACHE_SUFFIX) == "just a string"


def test_legacy_pickle_cache_is_a_miss(cache_dir):
    """旧版 `HMAC1` + pickle 缓存不得被解析——保留读取路径等于保留 RCE 原语。"""
    import pickle as _pickle
    payload = _pickle.dumps({"evil": len})
    key = cs._secret_key(cache_dir)
    sig = hmac.new(key, payload, hashlib.sha256).hexdigest()
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, "legacy.pkl"), "wb") as f:
        f.write(b"HMAC1" + b"\n" + sig.encode("ascii") + b"\n" + payload)
    assert cs.load_signed(cache_dir, "legacy.pkl") is None
    # 就算把旧载荷写成新后缀（魔数仍是 HMAC1）也必须拒绝
    with open(os.path.join(cache_dir, "legacy2" + cs.CACHE_SUFFIX), "wb") as f:
        f.write(b"HMAC1" + b"\n" + sig.encode("ascii") + b"\n" + payload)
    assert cs.load_signed(cache_dir, "legacy2" + cs.CACHE_SUFFIX) is None


def test_oversized_cache_file_is_a_miss(cache_dir, monkeypatch):
    """超大文件在读入内存前就被拒（防"塞一个大文件"变成一次内存分配）。"""
    monkeypatch.setattr(cs, "_MAX_CACHE_BYTES", 64)
    cs.save_signed(cache_dir, "big" + cs.CACHE_SUFFIX, {"pad": "x" * 500})
    assert cs.load_signed(cache_dir, "big" + cs.CACHE_SUFFIX) is None


def test_disk_lru_eviction(cache_dir):
    for i in range(8):
        cs.save_signed(cache_dir, f"lru_{i}" + cs.CACHE_SUFFIX, {"i": i})
    # 把前 4 个 mtime 改旧
    for i in range(4):
        p = os.path.join(cache_dir, f"lru_{i}" + cs.CACHE_SUFFIX)
        old = time.time() - 10_000
        os.utime(p, (old, old))
    removed = cs.enforce_disk_lru(cache_dir, max_files=6, max_total_mb=1024)
    assert removed == 2, f"LRU 应淘汰最旧的 2 个，实际淘汰 {removed}"
    assert not os.path.exists(os.path.join(cache_dir, "lru_0" + cs.CACHE_SUFFIX))
    assert os.path.exists(os.path.join(cache_dir, "lru_7" + cs.CACHE_SUFFIX))


def test_disk_lru_also_cleans_legacy_pkl(cache_dir):
    """旧 `.pkl` 已不再被读取，但仍应被 LRU 清掉，否则永远占磁盘。"""
    os.makedirs(cache_dir, exist_ok=True)
    for i in range(3):
        with open(os.path.join(cache_dir, f"old_{i}.pkl"), "wb") as f:
            f.write(b"HMAC1\n" + b"f" * 64 + b"\n{}")
    removed = cs.enforce_disk_lru(cache_dir, max_files=1, max_total_mb=1024)
    assert removed == 2, f"旧 .pkl 应参与淘汰，实际淘汰 {removed}"


# ------------------------------------------------------------------ #
#  路径穿越（P1.5 / 审查报告 H-5）
# ------------------------------------------------------------------ #
# 这一组是 P1.5 的回归防线。原实现只做 `os.path.join(cache_dir, filename)`，
# 而 filename 的一个来源是请求参数（页面级缓存键里嵌了 SolveReq.date）。
# `pd.to_datetime('2024-07-15/../../..')` **解析成功**，于是日期看着正常、
# 落盘位置却在 `.solve_cache` 之外——两个普通鉴权请求即可在仓库树里写出
# 一个带真实 HMAC 签名的文件，或压到另一天的缓存条目上（跨条目投毒）。
#
# 攻击面有两层，都要钉：文件名本身（本文件）与 date 入参（test_server_api.py）。
_TRAVERSAL_NAMES = [
    "../escape" + cs.CACHE_SUFFIX,                       # 相对上级
    "..\\escape" + cs.CACHE_SUFFIX,                      # Windows 分隔符
    "sub/child" + cs.CACHE_SUFFIX,                       # 子目录
    "sub\\child" + cs.CACHE_SUFFIX,
    "pe_abc_2024-07-15/../../.." + cs.CACHE_SUFFIX,      # 审查报告里那条真实载荷
    "pe_abc_x/../../.solve_cache/pe_victim" + cs.CACHE_SUFFIX,  # 跨条目投毒
    "..",                                                # 纯上级（目录本身）
    ".",                                                 # 当前目录
    "C:\\Windows\\Temp\\x" + cs.CACHE_SUFFIX,            # 盘符绝对路径
    "\\\\server\\share\\x" + cs.CACHE_SUFFIX,            # UNC
    "bad\x00name" + cs.CACHE_SUFFIX,                     # 空字节
    "",                                                  # 空名
]


@pytest.mark.parametrize("name", _TRAVERSAL_NAMES)
def test_save_signed_rejects_path_traversal(cache_dir, name):
    """写入侧必须直接拒（返回 False），且不得在 cache_dir 之外留下任何文件。"""
    parent = os.path.dirname(cache_dir)
    before = set(os.listdir(parent)) if os.path.isdir(parent) else set()
    assert cs.save_signed(cache_dir, name, {"who": "ATTACKER"}) is False, (
        f"非法文件名 {name!r} 被接受——文件名来源含请求参数，接受它等于"
        f"允许带真实签名的任意相对路径写入")
    after = set(os.listdir(parent)) if os.path.isdir(parent) else set()
    assert after == before, f"非法文件名 {name!r} 在缓存目录之外留下了 {sorted(after - before)}"


@pytest.mark.parametrize("name", _TRAVERSAL_NAMES)
def test_load_signed_rejects_path_traversal(cache_dir, name):
    """读取侧同样拒——否则读侧可被用来把外部文件当缓存内容取回。"""
    assert cs.load_signed(cache_dir, name) is None


def test_traversal_escape_is_not_written_outside_cache_dir(cache_dir):
    """审查报告 H-5 的原始载荷：手工造一个"签名完全正确"的越界文件，读取仍必须拒绝。

    这条与上面两条的分工：上面两条证明"写不出去/读不进来"，这条证明**即使攻击者
    绕过了写入侧**（比如拿到密钥后自己写文件），读取侧也不会去 cache_dir 外面取。
    判据是"文件真实存在且签名有效，但 load_signed 依然返回 None"。
    """
    parent = os.path.dirname(cache_dir)
    os.makedirs(parent, exist_ok=True)
    # 越界目标：cache_dir 的父目录下的一个文件，用**真实密钥**签好
    escape_name = ".._20_90_1000_True" + cs.CACHE_SUFFIX
    outside = os.path.join(parent, escape_name)
    _write_signed_raw(cache_dir, escape_name, json.dumps(
        {"who": "ATTACKER", "payload": "escaped"}).encode("utf-8"))
    # _write_signed_raw 把它写进了 cache_dir；搬到父目录模拟"越界已存在"
    os.replace(os.path.join(cache_dir, escape_name), outside)
    assert os.path.exists(outside), "前置条件不成立：越界文件没造出来"

    # 用穿越路径去读它——必须拒绝，而不是把父目录那个文件当缓存返回
    assert cs.load_signed(cache_dir, "../" + escape_name) is None, (
        "load_signed 顺着 '../' 读到了 cache_dir 之外的文件")
    # 同一份内容用纯文件名读（不穿越）时是**能**读出来的——证明拒绝的是路径，
    # 不是内容，避免"因为签名恰好不对所以碰巧通过"这种假绿。
    os.replace(outside, os.path.join(cache_dir, escape_name))
    got = cs.load_signed(cache_dir, escape_name)
    assert got == {"who": "ATTACKER", "payload": "escaped"}, (
        f"同内容文件在 cache_dir 内应能正常读出，实际 {got!r}；"
        f"说明上面的拒绝并非来自路径校验")
    os.remove(os.path.join(cache_dir, escape_name))


def test_safe_cache_name_accepts_real_world_keys(cache_dir):
    """不能为了防穿越把正常键也拒了——这里钉住两类真实键的形状。"""
    # 主求解键：md5 十六进制
    md5_key = hashlib.md5(b"whatever").hexdigest() + cs.CACHE_SUFFIX
    assert cs.save_signed(cache_dir, md5_key, {"v": 1}) is True
    assert cs.load_signed(cache_dir, md5_key) == {"v": 1}
    # 页面级键：pe_<fp8>_<日期>_<soc_min>_<soc_max>_<额定功率>_<use_ml>
    pe_key = "pe_3fcafe26_2024-07-15_20_90_1000_True" + cs.CACHE_SUFFIX
    assert cs.save_signed(cache_dir, pe_key, {"v": 2}) is True
    assert cs.load_signed(cache_dir, pe_key) == {"v": 2}
    # 点号名（`.json` 后缀之外还有 `.`）不应被误伤：文件名里出现点号是合法的
    assert cs.save_signed(cache_dir, "v1.2.4.json", {"v": 3}) is True
    assert cs.load_signed(cache_dir, "v1.2.4.json") == {"v": 3}
