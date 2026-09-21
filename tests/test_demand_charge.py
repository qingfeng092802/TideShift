"""基本电费（容/需量电价）计费的回归测试。

守的是三件事：
1. 1583号的**分段**判对——尤其是"未超过合同核定值 105% 时按核定值收取"这条
   反直觉的规则（它决定了储能需量收益是不是 0，写错就等于把收益白送给营销 PPT）；
2. 单价与阈值只有一份事实来源（config.DemandTariffConfig），本模块不许再抄；
3. 日峰值平均 ≤ 月峰值——按日均摊会低估月账单，这条性质一旦破了说明月峰跟踪写错了。
"""
import ast
import math
from pathlib import Path

import numpy as np
import pytest

from src.utils.config import CONFIG, DemandTariffConfig, SystemConfig
from src.utils.demand_charge import (
    basic_charge, daily_provision_yuan, demand_saving_yuan, effective_contract_kw,
    grid_import_kw, month_peak_kw, with_contract_kw, with_mode,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "utils" / "demand_charge.py"


def cfg(**kw):
    base = DemandTariffConfig()
    return DemandTariffConfig(**{**base.__dict__, **kw})


# ---------------------------------------------------------------- 1583号的分段

def test_contract_mode_charges_the_declared_value_below_the_band():
    """月峰在核定值 105% 以内 → 按**核定值**收，与削了多少峰无关。

    这条是整件事的关键：把月峰从 2300 削到 2000，核定值 2400 未变时一分钱不省。
    """
    r = basic_charge(2000.0, cfg=cfg(mode="contract", contract_demand_kw=2400.0))
    assert r.band_kw == pytest.approx(2520.0)
    assert r.over_band_kw == 0.0
    assert r.charge_yuan == pytest.approx(2400.0 * 36.1)


def test_contract_mode_doubles_only_the_part_above_105_percent():
    """超额部分加倍，未超额部分仍按核定值——分段点是 105%，不是核定值本身。"""
    c = cfg(mode="contract", contract_demand_kw=2000.0)      # 阈值 2100 kW
    at_band = basic_charge(2100.0, cfg=c)
    over = basic_charge(2200.0, cfg=c)                       # 超 100 kW
    assert at_band.over_band_kw == pytest.approx(0.0)
    assert over.over_band_kw == pytest.approx(100.0)
    assert over.charge_yuan - at_band.charge_yuan == pytest.approx(100.0 * 36.1 * 2.0)
    assert over.charge_yuan == pytest.approx(2000 * 36.1 + 2 * 100 * 36.1)


def test_declared_value_has_a_40_percent_capacity_floor():
    """申报值低于变压器容量总和 40% 时按 40% 核定——报低并不能少交。"""
    c = cfg(transformer_kva=3200.0, contract_demand_kw=200.0)
    assert effective_contract_kw(c) == pytest.approx(0.40 * 3200.0)
    assert basic_charge(100.0, cfg=c).charge_yuan == pytest.approx(1280.0 * 36.1)


def test_capacity_mode_ignores_the_peak_entirely():
    """按容量计费与月峰无关：这一档下储能省不了任何基本电费。"""
    lo = basic_charge(500.0, cfg=cfg(mode="capacity"))
    hi = basic_charge(5000.0, cfg=cfg(mode="capacity"))
    assert lo.charge_yuan == hi.charge_yuan == pytest.approx(3200.0 * 22.6)


def test_actual_mode_is_linear_in_the_peak():
    """按实际最大需量（2016 年之前的老口径）：每一 kW 月峰都值钱。"""
    r1 = basic_charge(2000.0, cfg=cfg(mode="actual"))
    r2 = basic_charge(2100.0, cfg=cfg(mode="actual"))
    assert r2.charge_yuan - r1.charge_yuan == pytest.approx(100.0 * 36.1)


def test_single_tariff_users_pay_no_basic_charge():
    """单一制用户表里那两栏是「/」，必须能表达成 0，而不是回落到需量制。"""
    assert basic_charge(2399.0, cfg=cfg(mode="none")).charge_yuan == 0.0


def test_unknown_mode_raises_instead_of_defaulting_to_zero():
    """落空即抛错，跟电价判档同一规矩：写错计费方式不能悄悄变成"不收钱"。"""
    with pytest.raises(ValueError):
        basic_charge(1000.0, cfg=cfg(mode="按心情"))


def test_saving_is_zero_until_the_month_peak_crosses_the_band():
    """需量节省在合同制下是阈值型，不是线性——这条断言钉住"别把 0 说成收益"。"""
    c = cfg(mode="contract", contract_demand_kw=2400.0)
    assert demand_saving_yuan(2399.0, 2100.0, cfg=c) == pytest.approx(0.0)
    # 顶到超额区以后再削，每 kW 值 2×单价
    assert demand_saving_yuan(2700.0, 2600.0, cfg=c) == pytest.approx(100.0 * 36.1 * 2)


# ------------------------------------------------------- 电网侧口径与月峰跟踪

def test_grid_import_drops_when_battery_discharges():
    """放电顶上去 → 电网取电下降；充电 → 上升。符号写反整份需量结论都作废。"""
    load = np.array([1000.0, 1200.0, 800.0])
    grid = grid_import_kw(load, charge_kw=[0, 200, 0], discharge_kw=[300, 0, 0])
    assert list(grid) == [700.0, 1400.0, 800.0]


def test_month_peak_is_never_below_the_mean_daily_peak():
    """月峰 ≥ 日峰均值：所以"按当天峰值×单价÷月天数"日度计费会**低估**月账单。"""
    days = [np.array([100.0, 90.0]), np.array([200.0, 150.0]), np.array([120.0, 110.0])]
    peak = month_peak_kw(days)
    mean_daily = float(np.mean([d.max() for d in days]))
    assert peak == pytest.approx(200.0)
    assert peak >= mean_daily


def test_daily_provision_sums_back_to_the_month_bill():
    month_bill = basic_charge(2399.0, cfg=cfg(mode="contract")).charge_yuan
    per_day = daily_provision_yuan(month_bill, 30)
    assert per_day * 30 == pytest.approx(month_bill)
    with pytest.raises(ValueError):
        daily_provision_yuan(month_bill, 0)


# --------------------------------------------------------------- 单一事实来源

def test_prices_are_not_copied_into_this_module():
    """单价只许写在 config.DemandTariffConfig 一处。

    模块 docstring 里引用 36.1 / 22.6 是**出处记录**，不算第二份事实来源，
    所以只扫 docstring 与注释之外的代码行——与 tests/test_price_calendar.py
    守 server.py「不要再抄一份时段文字」同一套做法。
    """
    raw = SRC.read_text(encoding="utf-8")
    tree = ast.parse(raw)
    lines = raw.splitlines()
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        lines = lines[tree.body[0].end_lineno:]
    code = "\n".join(l for l in lines if not l.strip().startswith("#"))
    for stale in ("36.1", "22.6", "0.40 *", "1.05", "2.0 *"):
        assert stale not in code, f"demand_charge.py 的代码里出现了抄写的数字：{stale}"


def test_helpers_rebuild_a_frozen_config_instead_of_mutating_it():
    """frozen dataclass 只能复制着改；这里守的是"换档/扫核定值不污染全局 CONFIG"。"""
    system = SystemConfig()
    swapped = with_mode(system.demand, "actual")
    swept = with_contract_kw(swapped, 1800.0)
    assert swapped.mode == "actual" and system.demand.mode == "contract"
    assert swept.contract_demand_kw == 1800.0 and swapped.contract_demand_kw != 1800.0
    with pytest.raises(ValueError):
        with_mode(system.demand, "不存在的档")


def test_resume_only_skips_days_it_can_actually_reuse():
    """续跑判据：字段齐、且需量上限一致，才许跳过。

    踩过的坑：`--no-milp --emit X` 先写满 30 行（只有峰值），随后真正带 MILP 的命令
    按"日期已存在"把它们全跳过——一圈跑完什么优化结果都没有。另一侧同样危险：
    开环的行被闭环运行复用，等于拿别人的解冒充自己算出来的。
    """
    from src.utils.demand_charge import _row_is_reusable

    full = {"date": "2024-07-01", "load_peak_kw": 1.0, "base_peak_kw": 1.0,
            "opt_peak_kw": 2.0, "opt_net_yuan": 3.0}
    norev = {"date": "2024-07-02", "load_peak_kw": 1.0, "base_peak_kw": 1.0}
    capped = {**full, "opt_cap_kw": 2500.0}

    assert _row_is_reusable(full, run_milp=True, demand_cap_kw=None)
    assert not _row_is_reusable(norev, run_milp=True, demand_cap_kw=None)   # 缺优化列
    assert _row_is_reusable(norev, run_milp=False, demand_cap_kw=None)      # 只算峰值够用
    assert not _row_is_reusable(full, run_milp=True, demand_cap_kw=2500.0)  # 开环≠闭环
    assert not _row_is_reusable(capped, run_milp=True, demand_cap_kw=None)
    assert _row_is_reusable(capped, run_milp=True, demand_cap_kw=2500.0)


def test_real_dataset_month_peak_matches_the_reported_number():
    """内置 7 月的无储能月峰就是负荷月峰（2399 kW 量级）。

    这个数是阶段 2 全部结论的锚点，数据重新生成后如果它变了，
    文档里的需量结论必须一起重测，所以在此钉住量级。
    """
    from src.data.data_loader import get_day_data, load_load_data

    df = load_load_data()
    days = sorted({t.date().isoformat() for t in df["timestamp"]})
    grids = [get_day_data(df, d)["load_kw"].to_numpy(dtype=float) for d in days]
    assert month_peak_kw(grids) == pytest.approx(float(df["load_kw"].max()), rel=1e-9)
    # 容量制没有"105% 阈值"这个概念，band 必须是 nan 而不是 0——0 会被下游当成"已经超额"
    assert math.isnan(basic_charge(1.0, cfg=cfg(mode="capacity")).band_kw)
    assert not math.isnan(basic_charge(1.0, cfg=cfg(mode="contract")).band_kw)


# --------------------------------------------- 闭环：需量上限进 MILP（快测那半）

def test_demand_cap_requires_the_load_curve():
    """只给上限不给负荷 → 抛错。

    这是本仓库对付"静默退化"的固定手法：少了负荷曲线，
    `load + P_charge − P_discharge ≤ cap` 会退化成只约束电池净功率，
    看着像在管需量、其实管的是完全不同的东西。
    """
    from src.agents.storage_optimization_agent import StorageOptimizationAgent

    agent = StorageOptimizationAgent(CONFIG)
    price = np.full(96, 0.65)
    with pytest.raises(ValueError, match="成对"):
        agent.optimize(price, demand_cap_kw=2500.0)
    with pytest.raises(ValueError, match="成对"):
        agent.optimize(price, load_profile_kw=np.full(96, 1200.0))


def test_demand_cap_load_length_is_checked():
    """负荷长度不是 96 时不许按位置错配到别处去。"""
    from src.agents.storage_optimization_agent import StorageOptimizationAgent

    agent = StorageOptimizationAgent(CONFIG)
    price = np.full(96, 0.65)
    with pytest.raises(ValueError, match="长度"):
        agent.optimize(price, load_profile_kw=np.full(24, 1200.0), demand_cap_kw=2500.0)


def test_grid_peak_field_distinguishes_not_computed_from_zero():
    """不开需量管理时 grid_peak_kw 是 0.0，含义是"没算过"。

    默认值选 0 而不是 nan 是历史习惯，所以这里把语义钉住：真算出来的电网峰值
    不可能为 0（有负荷就有取电），任何下游判断都不许把 0.0 当成"峰值为 0"来用。
    这里刻意不求解——只钉字段默认值，求解那条在 test_capped_schedule_never_exceeds_the_cap。
    """
    from src.agents.storage_optimization_agent import ScheduleResult

    fields = ScheduleResult.__dataclass_fields__
    assert fields["grid_peak_kw"].default == 0.0
    assert fields["demand_cap_kw"].default == 0.0


@pytest.mark.slow
def test_capped_schedule_never_exceeds_the_cap():
    """闭环的硬指标：日内任意 15 分钟电网取电都不许越过上限。

    开环在同一工况下会把日峰从 2274 kW 顶到 2774 kW（谷段尾部满功率充电
    叠在早爬坡负荷上），闭环必须既压住峰值、又不塌掉收益。
    """
    from src.agents.storage_optimization_agent import StorageOptimizationAgent
    from src.data.data_loader import day_price_temp, load_load_data
    from src.utils.demand_charge import grid_import_kw

    df = load_load_data()
    date = "2024-07-01"
    load = df[df["timestamp"].dt.date.astype(str) == date]["load_kw"].to_numpy(float)
    price, ambient = day_price_temp(df, date)
    agent = StorageOptimizationAgent(CONFIG)
    cap = 2500.0

    open_res = agent.optimize(price, ambient_temp_profile=ambient)
    open_peak = float(grid_import_kw(load, open_res.charge_power_kw,
                                     open_res.discharge_power_kw).max())
    assert open_peak > cap, "开环若不再顶峰，这条测试与它守的那条结论要一起重审"

    capped = agent.optimize(price, ambient_temp_profile=ambient,
                            load_profile_kw=load, demand_cap_kw=cap)
    grid = grid_import_kw(load, capped.charge_power_kw, capped.discharge_power_kw)
    assert float(grid.max()) <= cap + 1e-6
    assert capped.grid_peak_kw == pytest.approx(float(grid.max()), abs=1e-6)
    assert capped.demand_cap_kw == pytest.approx(cap)
    # 上限只该拿走很小的套利：真吐回一大半说明约束写错了（比如把负荷漏掉）
    assert capped.arbitrage_revenue_yuan > open_res.arbitrage_revenue_yuan * 0.9
