"""
核心调度模块测试（pytest）

原来这个文件只有 print、没有一条 assert，改坏了代码不会报警。
现在覆盖真正会被问到的性质：能量守恒、SOC 不越界、终值约束、热约束生效、
优化不劣于基准、SOC 区间加权衰减确实提升决策质量。

运行：
    pytest tests/test_storage_agent.py -v
    python tests/test_storage_agent.py
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.config import CONFIG, internal_resistance_from_efficiency
from src.agents.storage_optimization_agent import StorageOptimizationAgent
from src.data.data_generator import generate_price_profile, generate_ambient_temp

# 真实 MILP/全流程测试，标记 slow，PR CI 默认跳过
pytestmark = pytest.mark.slow

CFG = CONFIG
BAT = CFG.battery


def _profiles(date="2024-07-30"):
    """当日电价与气温：优先取内置数据集里那一天真正落盘的序列。

    不直接调 `generate_*`：`generate_ambient_temp` 内含一行**未播种**的高斯噪声，
    每次 import 本模块就换一条气温曲线，而本文件的断言余量比这种抽样漂移还小
    （区间加权 vs 常数近似的净收益差实测 +2.93%）——那样测试等于掷骰子。
    """
    from src.data.data_loader import day_price_temp, load_load_data
    pt = day_price_temp(load_load_data(), date)
    if pt is not None:
        return pt
    tidx = pd.date_range(date, periods=96, freq="15min")
    return (np.asarray(generate_price_profile(tidx), dtype=float),
            np.asarray(generate_ambient_temp(tidx), dtype=float))


PRICE, AMB = _profiles()


@pytest.fixture(scope="module")
def agent():
    return StorageOptimizationAgent(CFG)


# ------------------------------------------------------------------ #
#  物理参数自洽性
# ------------------------------------------------------------------ #
def test_internal_resistance_consistent_with_efficiency():
    """内阻必须由效率反推：额定点 I²R 损耗 == P(1-η)，否则与 SOC 能量方程重复计损"""
    r = internal_resistance_from_efficiency(BAT.charge_efficiency,
                                            BAT.nominal_voltage_v, BAT.rated_power_kw)
    assert abs(BAT.internal_resistance_ohm - r) < 1e-6
    i = BAT.rated_power_kw * 1000 / BAT.nominal_voltage_v
    loss_kw = i ** 2 * BAT.internal_resistance_ohm / 1000
    assert loss_kw == pytest.approx(BAT.rated_power_kw * (1 - BAT.charge_efficiency), rel=1e-6)


def test_thermal_params_in_engineering_range():
    """热容/热阻必须在 2MWh 集装箱的物理量级内（原实现分别差 231 倍和 35 倍）"""
    assert 5000 <= BAT.thermal_capacity_kj_k <= 40000, "热容应在数千~数万 kJ/K"
    assert 0.0002 <= BAT.thermal_resistance_k_w <= 0.005, "热阻应对应 0.2~5 kW/K 散热能力"
    tau_h = BAT.thermal_capacity_kj_k * 1000 * BAT.thermal_resistance_k_w / 3600
    assert 0.5 <= tau_h <= 10, f"热时间常数 {tau_h:.2f}h 应在 0.5~10h"


# ------------------------------------------------------------------ #
#  能量守恒 / 约束可行性
# ------------------------------------------------------------------ #
def test_optimized_schedule_conserves_energy(agent):
    """SOC 轨迹必须与充放电吞吐严格一致（原实现 96 步里 88 步越界，靠 np.clip 掩盖）"""
    r = agent.optimize(PRICE, AMB, initial_soc=0.5, terminal_soc="cyclic")
    assert r.solver_status == "Optimal"
    assert r.soc_violation_steps == 0, f"{r.soc_violation_steps} 步 SOC 越界"
    assert r.energy_balance_error_kwh < 1e-6, f"能量残差 {r.energy_balance_error_kwh} kWh"


def test_optimized_soc_within_bounds(agent):
    r = agent.optimize(PRICE, AMB, initial_soc=0.5, terminal_soc="cyclic")
    assert r.soc.min() >= BAT.soc_min - 1e-6
    assert r.soc.max() <= BAT.soc_max + 1e-6


def test_terminal_soc_constraint_holds(agent):
    """稳态口径：末 SOC 必须等于初 SOC，否则日收益是吃初始存量换来的，不可持续"""
    r = agent.optimize(PRICE, AMB, initial_soc=0.5, terminal_soc="cyclic")
    assert r.terminal_soc == pytest.approx(0.5, abs=1e-6)
    r2 = agent.optimize(PRICE, AMB, initial_soc=0.35, terminal_soc=0.7)
    assert r2.terminal_soc == pytest.approx(0.7, abs=1e-6)


def test_no_simultaneous_charge_discharge(agent):
    """同一时段不能既充又放（用一条线性约束替代了原来的 96 个二进制变量）"""
    r = agent.optimize(PRICE, AMB, initial_soc=0.5, terminal_soc="cyclic")
    both = np.minimum(r.charge_power_kw, r.discharge_power_kw)
    assert both.max() < 1e-6, "存在同时充放电的时段"
    assert (r.charge_power_kw + r.discharge_power_kw).max() <= BAT.rated_power_kw + 1e-6


# ------------------------------------------------------------------ #
#  热约束
# ------------------------------------------------------------------ #
def test_thermal_constraint_is_effective(agent):
    """带热约束的解，后验仿真温度必须不超过正常上限"""
    r = agent.optimize(PRICE, AMB, initial_soc=0.5, terminal_soc="cyclic",
                       include_thermal_constraint=True)
    assert r.max_battery_temp_c <= BAT.temp_normal_max + 0.5, \
        f"优化解最高温 {r.max_battery_temp_c}℃ 超过阈值 {BAT.temp_normal_max}℃"


def test_thermal_constraint_has_cost(agent):
    """去掉热约束会显著推高温度 —— 说明这条约束不是摆设"""
    with_t = agent.optimize(PRICE, AMB, initial_soc=0.5, terminal_soc="cyclic",
                            include_thermal_constraint=True)
    no_t = agent.optimize(PRICE, AMB, initial_soc=0.5, terminal_soc="cyclic",
                          include_thermal_constraint=False)
    assert no_t.max_battery_temp_c > with_t.max_battery_temp_c + 1.0, \
        "热约束没有起到降温作用"
    assert no_t.net_revenue_yuan >= with_t.net_revenue_yuan - 1e-6, \
        "去掉约束后收益反而更低，说明模型有问题"


def test_heat_generation_is_quadratic_not_linear():
    """产热必须随功率平方增长（I²R），分段线性化的弦必须始终不低于真实曲线"""
    from src.utils.config import heat_pwl_breakpoints
    p_pts, q_pts = heat_pwl_breakpoints(BAT, 6)
    k = BAT.internal_resistance_ohm * (1 + BAT.reaction_heat_coeff) * 1e6 / BAT.nominal_voltage_v ** 2
    for i in range(len(p_pts) - 1):
        p0, p1, q0, q1 = p_pts[i], p_pts[i + 1], q_pts[i], q_pts[i + 1]
        for frac in (0.25, 0.5, 0.75):
            p = p0 + frac * (p1 - p0)
            chord = q0 + frac * (q1 - q0)
            true_q = k * p ** 2
            assert chord >= true_q - 1e-6, \
                f"弦在 P={p:.1f}kW 处低于真实产热，热约束将不再安全（{chord:.1f} < {true_q:.1f}）"


# ------------------------------------------------------------------ #
#  基准策略
# ------------------------------------------------------------------ #
def test_baseline_is_physically_feasible(agent):
    """基准策略同样必须满足能量守恒与 SOC 边界（原实现两项都不满足）"""
    b = agent.baseline_strategy(PRICE, AMB, initial_soc=0.5, terminal_soc="cyclic")
    assert b.soc_violation_steps == 0
    assert b.soc.min() >= BAT.soc_min - 1e-6
    assert b.soc.max() <= BAT.soc_max + 1e-6
    assert b.terminal_soc == pytest.approx(0.5, abs=1e-6)


def test_baseline_uses_the_cheapest_and_dearest_hours(agent):
    """基准必须在最低价档充电、在最高价档放电（原实现只吃到高峰档，放过了尖峰）"""
    b = agent.baseline_strategy(PRICE, AMB, initial_soc=0.5, terminal_soc="cyclic")
    charged = PRICE[b.charge_power_kw > 1e-6]
    discharged = PRICE[b.discharge_power_kw > 1e-6]
    assert len(charged) > 0 and len(discharged) > 0
    assert charged.max() <= PRICE.min() + 1e-9, "基准在最便宜的时段以外充了电"
    assert discharged.max() == pytest.approx(PRICE.max(), abs=1e-9), \
        "基准没有在最高价（尖峰）时段放电，又是稻草人"


def test_optimizer_beats_baseline(agent):
    """MILP 优化必须不劣于人工规则基准（同口径、稳态）"""
    b = agent.baseline_strategy(PRICE, AMB, initial_soc=0.5, terminal_soc="cyclic")
    o = agent.optimize(PRICE, AMB, initial_soc=0.5, terminal_soc="cyclic")
    assert o.net_revenue_yuan >= b.net_revenue_yuan - 1e-6, \
        f"优化 {o.net_revenue_yuan} 反而低于基准 {b.net_revenue_yuan}"


# ------------------------------------------------------------------ #
#  寿命衰减建模
# ------------------------------------------------------------------ #
def test_soc_weighted_degradation_improves_decisions(agent):
    """
    SOC 区间加权让优化器看得见真实的成本结构（高SOC/低SOC 吞吐更贵），
    因此按真实成本结算时，它的净收益不应低于"拍一个常数系数"的近似模型。
    """
    flat = agent.optimize(PRICE, AMB, initial_soc=0.5, terminal_soc="cyclic",
                          soc_weighted_degradation=False)
    weighted = agent.optimize(PRICE, AMB, initial_soc=0.5, terminal_soc="cyclic",
                              soc_weighted_degradation=True)
    assert flat.solver_status == "Optimal" and weighted.solver_status == "Optimal"
    assert weighted.net_revenue_yuan >= flat.net_revenue_yuan - 1e-6, \
        f"精确衰减模型({weighted.net_revenue_yuan}) 反而差于常数近似({flat.net_revenue_yuan})"


def test_reported_degradation_is_self_consistent(agent):
    """README 里的年衰减率必须由日衰减成本推出，不能与代码输出差 2 倍"""
    r = agent.optimize(PRICE, AMB, initial_soc=0.5, terminal_soc="cyclic")
    per_cycle_cost = BAT.total_battery_cost / BAT.cycle_life
    cycles_per_year = r.degradation_cost_yuan / per_cycle_cost * 365
    annual_rate = cycles_per_year / BAT.cycle_life * 100
    assert 0 < annual_rate < 20, f"年衰减率 {annual_rate:.2f}% 不合理"
    print(f"\n  [参考] 日衰减成本 {r.degradation_cost_yuan} 元 → "
          f"年衰减率 {annual_rate:.2f}%，折合寿命 {100/annual_rate:.1f} 年")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
