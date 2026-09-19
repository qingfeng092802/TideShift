# -*- coding: utf-8 -*-
"""电价日历的回归测试。

存在的理由：本项目的"广东工商业峰谷分时电价"此前有四套互相矛盾的时段口径
（生成器、上传适配、后端 params 默认值、后端 price_cards 展示），且尖峰全年生效。
这些测试把 331 号文的口径钉成断言，并守住"单一事实来源"不再被抄第二份。
"""
import random

import numpy as np
import pandas as pd
import pytest

from src.data import data_generator as dg
from src.data.data_generator import PRICE_PERIODS, price_by_hour
from src.data.upload_adapter import default_price_by_hour
from src.utils.config import CONFIG

SPIKE = CONFIG.price.spike_price
PEAK = CONFIG.price.peak_price
FLAT = CONFIG.price.flat_price
VALLEY = CONFIG.price.valley_price

ALL_MONTHS = list(range(1, 13))
QUARTER_HOURS = [i * 0.25 for i in range(96)]


# ---------------------------------------------------------------- 覆盖性 ----
@pytest.mark.parametrize("month", ALL_MONTHS)
def test_no_gap_in_the_calendar(month):
    """每个季度的每一刻都必须落进某一档。

    原实现在落不到任何区间时**静默返回平段价**——时段表被改漏时不会报错，
    只会让一部分小时悄悄变成平段，进而悄悄改变收益。
    """
    for h in QUARTER_HOURS:
        assert price_by_hour(h, month=month) in (SPIKE, PEAK, FLAT, VALLEY)


def test_every_hour_resolves_to_exactly_one_tier():
    """同一个月里，一个小时不应同时命中两档且价格不同（窗口重叠=口径歧义）。"""
    for month in ALL_MONTHS:
        for h in range(24):
            hits = []
            for per in PRICE_PERIODS:
                months = per.get("months")
                if months is not None and month not in months:
                    continue
                if any(lo <= h < hi for lo, hi in per["range"]):
                    hits.append(per["name"])
            nested = [n for n in hits if n != "高峰"]
            assert len(nested) <= 1, f"{month}月 {h}点 命中多档: {hits}"


def test_widest_match_does_not_depend_on_list_order():
    """尖峰窗口是高峰窗口的子集，匹配必须是"最窄者胜"，不能靠列表顺序。

    原实现是 for + break 的第一个命中即返回，一旦有人把尖峰挪到高峰之前
    （或反过来）就会静默换档。这里把表打乱重算，逐点对比。
    """
    rng = random.Random(7)
    baseline = [price_by_hour(h, month=m) for m in ALL_MONTHS for h in QUARTER_HOURS]
    original = dg.PRICE_PERIODS
    order = original[:]
    for _ in range(5):
        rng.shuffle(order)
        dg.PRICE_PERIODS = order
        try:
            again = [price_by_hour(h, month=m) for m in ALL_MONTHS for h in QUARTER_HOURS]
        finally:
            dg.PRICE_PERIODS = original
        assert again == baseline, f"打乱顺序后档位变了: {[p['name'] for p in order]}"


# ------------------------------------------------------------ 331 号文 ----
def test_valley_window_is_00_to_08():
    """文件：低谷时段为 0-8 点。此前实现只给到 0-7 点。"""
    assert price_by_hour(7.75, month=1) == VALLEY
    assert price_by_hour(8.0, month=1) == FLAT


def test_spike_months_are_july_august_september_only():
    """文件：尖峰电价执行时间为 7、8、9 月三个整月。"""
    for m in (7, 8, 9):
        assert price_by_hour(11.5, month=m) == SPIKE, f"{m}月 11:30 应为尖峰"
        assert price_by_hour(16.0, month=m) == SPIKE, f"{m}月 16:00 应为尖峰"
    for m in (1, 2, 3, 4, 5, 6, 10, 11, 12):
        assert price_by_hour(11.5, month=m) == PEAK, f"{m}月 11:30 不该有尖峰"
        assert price_by_hour(16.0, month=m) == PEAK, f"{m}月 16:00 不该有尖峰"


def test_spike_windows_are_11_12_and_15_17_only():
    """文件：尖峰每天执行 11-12 时、15-17 时共三小时。此前实现多算了 19-21 时。"""
    assert price_by_hour(19.5, month=7) == FLAT      # 19-24 是平段
    assert price_by_hour(20.5, month=7) == FLAT
    assert price_by_hour(14.75, month=7) == PEAK     # 14-19 高峰，15 点起转尖峰
    assert price_by_hour(12.0, month=7) == FLAT      # 12-14 平段


def test_peak_windows_are_10_12_and_14_19():
    assert price_by_hour(9.5, month=7) == FLAT       # 8-10 平段
    assert price_by_hour(10.5, month=7) == PEAK
    assert price_by_hour(13.0, month=7) == FLAT      # 12-14 平段
    assert price_by_hour(18.5, month=7) == PEAK      # 14-19 高峰
    assert price_by_hour(23.5, month=7) == FLAT


def test_price_ratios_follow_the_document():
    """峰:平:谷 = 1.7:1:0.38，尖峰在高峰上浮 25%。"""
    assert PEAK / FLAT == pytest.approx(1.7, abs=1e-9)
    assert VALLEY / FLAT == pytest.approx(0.38, abs=1e-9)
    assert SPIKE / PEAK == pytest.approx(1.25, abs=1e-9)


def test_no_month_context_never_assumes_spike():
    """调用方拿不到月份时，宁可少收不多收：按高峰计，不判尖峰。"""
    assert price_by_hour(11.5) == PEAK
    assert price_by_hour(16.0) == PEAK


def test_hour_out_of_range_raises():
    with pytest.raises(ValueError):
        price_by_hour(24.0, month=7)
    with pytest.raises(ValueError):
        price_by_hour(-0.25, month=7)


def test_price_cfg_cannot_be_passed_positionally():
    """month/price_cfg 必须是关键字参数。

    老签名 `price_by_hour(h, price_cfg)` 若被改成 `price_by_hour(h, month, price_cfg)`，
    所有 `price_by_hour(h, cfg)` 的调用会把配置对象当成月份——不报错，静默算错价。
    """
    with pytest.raises(TypeError):
        price_by_hour(11.5, CONFIG.price)


# ------------------------------------------------------- 两条路径要一致 ----
def test_generator_and_upload_adapter_agree():
    """内置数据生成路径与上传数据路径必须给同一张日历。

    这两处曾经分叉过：24 小时里 11 小时档位不同、日均价差 23%，
    且分叉曲线含 6 小时连续同价区间，把 MILP 求解从 7.2 s 拖到 120 s 撞满时限。
    """
    for ts in (pd.Timestamp("2024-07-30 11:30"), pd.Timestamp("2024-01-15 11:30"),
               pd.Timestamp("2024-11-05 19:15"), pd.Timestamp("2024-08-01 07:45")):
        assert default_price_by_hour(ts) == price_by_hour(
            ts.hour + ts.minute / 60.0, month=ts.month), ts


def test_upload_adapter_is_month_aware():
    """上传适配此前只传 `.dt.hour`，等于让上传数据的日历退化成不分月份。"""
    july = pd.Timestamp("2024-07-30 11:30")
    january = pd.Timestamp("2024-01-15 11:30")
    assert default_price_by_hour(july) == SPIKE
    assert default_price_by_hour(january) == PEAK


def test_generated_profile_matches_pointwise_lookup():
    idx = pd.date_range("2024-07-30", periods=96, freq="15min")
    profile = np.asarray(dg.generate_price_profile(idx))
    expected = [price_by_hour(t.hour + t.minute / 60.0, month=t.month) for t in idx]
    assert profile == pytest.approx(expected)


def test_backend_has_no_second_copy_of_the_windows():
    """守住"不要再抄一份时段文字"。

    后端 `price_cards` 曾自带一套时段说明（"高峰 08-10, 18-21" 等），
    与引擎实际计价用的划分不同——用户在设置页看到的时段和真正算钱的时段对不上。
    """
    from pathlib import Path
    raw = (Path(__file__).resolve().parents[1] / "backend" / "server.py").read_text(
        encoding="utf-8")
    # 只看代码行：注释里引用旧口径是"为什么改"的记录，不是第二份事实来源
    code = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("#"))
    for stale in ("08-10, 18-21", "07-08, 12-18", "00-07, 21-24"):
        assert stale not in code, f"server.py 里又出现抄写的时段口径：{stale}"


# ------------------------------------------- 输入同源：不许再有两套私有日历/曲线 ----
def _code_lines(rel_path):
    from pathlib import Path
    raw = (Path(__file__).resolve().parents[1] / rel_path).read_text(encoding="utf-8")
    keep = [l for l in raw.splitlines()
            if not l.strip().startswith(("#", "//", "*"))]
    return "\n".join(keep)


def test_frontend_download_template_uses_the_dispatched_calendar():
    """前端「下载模板」曾内联第五套日历：`h >= 17 && h < 22 ? 1.35 : ...`。

    用户下载模板、只改负荷再传回来，算钱用的档位就跟引擎对不上，而且传回来的价格列
    会直接覆盖内置日历。现在模板按 /api/bootstrap 下发的 price_periods 算价。
    """
    code = _code_lines("web/js/app.js")
    assert "price_periods" in code, "模板不再读引擎下发的时段表，等于回去抄一份"
    # 只查电价字面量：负荷列里 `h >= 17 && h < 22` 那处是晚高峰**负荷**形状，与档位无关
    for stale in ("? 1.35", "0.86", ": 0.32"):
        assert stale not in code, f"app.js 里又出现抄写的电价：{stale}"


def test_optimize_binds_the_ambient_curve_not_a_dead_load_slot():
    """`optimize()` 的第二位必须真的是气温曲线。

    它曾挂在 `load_profile`（从未被读取）上，于是全仓 11 处 `optimize(PRICE, AMB)`
    位置调用把气温丢给了那个死参数，模型改用常量 30 ℃——热约束被静默放松，
    基于该调用测出来的历史数字（含"区间加权只多 0.14%"这个空结论）也随之失真。
    """
    import inspect

    from src.agents.storage_optimization_agent import StorageOptimizationAgent
    params = list(inspect.signature(StorageOptimizationAgent.optimize).parameters)
    assert "load_profile" not in params, f"死参数又回来了：{params}"
    assert params[:3] == ["self", "price_profile", "ambient_temp_profile"], params


def test_search_inputs_do_not_consume_randomness():
    """寻优路径不许再现场抽气温。

    `generate_ambient_temp` 内含一行未播种的高斯噪声，`_day_series` 此前直接调它，
    于是每新建一个 search agent 就是另一天天气：24 点默认约束连跑四次落在
    1352.18 ~ 1392.01 元（极差 2.9%，**大于 2% 的胜出门槛**）。这被误记成
    "mip_gap=1% 的求解器抖动"写进了注释与 README，实际差的从来是输入。
    """
    from src.agents.parameter_search_agent import ParameterSearchAgent
    from src.data.data_loader import load_load_data
    from src.utils.config import CONFIG

    hist = load_load_data()
    before = np.random.get_state()[1]
    ParameterSearchAgent(CONFIG)._day_series("2024-07-30", hist, 24)
    after = np.random.get_state()[1]
    assert np.array_equal(before, after), "准备输入时动了全局随机流——同配置重复求解不再可比"

