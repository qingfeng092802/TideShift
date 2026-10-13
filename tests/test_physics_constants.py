"""
物理常数与价格口径的**绝对值**守卫（快档，不跑 MILP）。

补于 2026-10-12：`tests/test_experiments_repro.py` 虽然钉住了 README 的 5 个
headline 数字，但它整文件带 `pytestmark = pytest.mark.slow`，而 `pytest.ini` 的
`addopts = -m "not slow"` 让 PR 快档默认跳过它。后果是**改物理常数不会让快档变红**：
实测把 `soc_min` 从 0.20 改成 0.15，快档 300 项全绿，只有慢档 4 项报红。

本文件把「常数本身」钉在快档里。它不跑求解器，秒级完成，代价接近零；
与 `test_experiments_repro.py` 的分工是：
- 本文件回答「常数被改了吗」——改一个数就红，且失败信息直接指出改的是哪一个；
- `test_experiments_repro.py` 回答「改完之后 README 那一列还对得上吗」——跑真 MILP。

两层都要有：只有慢档时，改常数在 PR 阶段完全无声；只有快档时，常数没改但
求解器换版导致结果漂移也无人发现。

⚠️ 这些是**业务口径**不是实现细节，改动必须同时更新 `docs/experiments.md`
§复现脚本与 README 量化成果表，否则本文件红得对、文档错得静默。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.config import (
    CONFIG,
    heat_pwl_breakpoints,
    internal_resistance_from_efficiency,
)

BAT = CONFIG.battery
PRICE = CONFIG.price


# ------------------------------------------------------------------ #
#  电池物理常数
# ------------------------------------------------------------------ #
def test_soc_window_is_pinned():
    """SOC 窗口是所有收益与衰减数字的分母，必须钉死。

    实测教训：`soc_min 0.20 → 0.15` 时快档全绿、慢档 4 项红
    （套利 1816.27→1879.06、衰减 388.37→454.17、温度 46.92→48.11、
    循环 1.4564→1.7031）。窗口放宽等于偷偷多放 100 kWh 电量出来。
    """
    assert BAT.soc_min == pytest.approx(0.2, abs=1e-12), (
        "SOC 下限被改动。它同时决定可用电量、衰减区间划分（0.2 以下是 3.0 倍系数区）"
        "与 README 全部收益数字；改它必须同步重算 docs/experiments.md §复现脚本")
    assert BAT.soc_max == pytest.approx(0.9, abs=1e-12), (
        "SOC 上限被改动。0.8~1.0 区间衰减系数是 2.0，抬高上限会让优化器"
        "在更贵的高 SOC 区吞吐，收益与衰减同时变")


def test_thermal_parameters_are_pinned():
    """一阶 RC 热模型三参数：热容、热阻、内阻。

    这三个数决定最高温 46.92 ℃ 与"热约束让出 3.3% 收益"的结论。
    `test_storage_agent.py::test_thermal_params_in_engineering_range` 只查量级区间，
    常数翻倍都还在区间内；这里钉到具体值。
    """
    assert BAT.thermal_capacity_kj_k == pytest.approx(15000.0, rel=1e-12), (
        "热容被改动。2MWh 集装箱约 13~20 吨电芯 × ~1 kJ/(kg·K)")
    assert BAT.thermal_resistance_k_w == pytest.approx(0.001, rel=1e-12), (
        "热阻被改动（等效散热能力 1.0 kW/K，代表空调失效的最坏校核工况）")
    assert BAT.internal_resistance_ohm == pytest.approx(0.018, rel=1e-12), (
        "内阻被改动。它由 (1-η)·U²/P_rated 反推，与 SOC 能量方程共用同一个损耗口径，"
        "手改会与效率重复计损")


def test_internal_resistance_is_derived_from_efficiency():
    """内阻不是独立参数，是效率的派生量——两者必须始终自洽。"""
    r = internal_resistance_from_efficiency(
        BAT.charge_efficiency, BAT.nominal_voltage_v, BAT.rated_power_kw)
    assert BAT.internal_resistance_ohm == pytest.approx(r, rel=1e-9), (
        f"内阻 {BAT.internal_resistance_ohm} 与由效率反推的 {r:.6f} 不一致："
        f"额定点 I²R 损耗不再等于 P(1-η)，会与 SOC 能量方程重复计损")


def test_efficiency_is_pinned():
    assert BAT.charge_efficiency == pytest.approx(0.95, abs=1e-12)
    assert BAT.discharge_efficiency == pytest.approx(0.95, abs=1e-12)


def test_thermal_time_constant_is_pinned():
    """τ = R_th·C。它决定"DR 窗口结束后温度还会继续爬"这个已知现象的量级。"""
    tau_h = BAT.thermal_capacity_kj_k * 1000 * BAT.thermal_resistance_k_w / 3600
    assert tau_h == pytest.approx(4.1667, abs=1e-3), (
        f"热时间常数 {tau_h:.4f} h 偏离 README 记载的 4.17 h。"
        f"它决定热惯性，进而决定后验峰值温度与 DR 的越限判断")


def test_temperature_thresholds_are_pinned():
    """三档温度阈值：45 降额线、55 停机线、60 告警线。"""
    assert BAT.temp_normal_max == pytest.approx(45.0, abs=1e-12), (
        "降额线被改动。DR 页的 temp_pass 判定与优化器的硬约束都用它")
    assert BAT.temp_safe_max == pytest.approx(55.0, abs=1e-12), (
        "停机线被改动。'无热约束上界 57.23 ℃ 越过 55 ℃ 停机阈值'这个结论依赖它")
    assert BAT.temp_critical == pytest.approx(60.0, abs=1e-12)


def test_heat_generation_coefficient_is_pinned():
    """产热系数 k = R·(1+reaction_heat)·1e6 / U²，单位 W per kW²。

    它是「46.92 ℃」与 SOS2 弦逼近误差 364.58 W 的共同源头。
    """
    k = BAT.internal_resistance_ohm * (1 + BAT.reaction_heat_coeff) * 1e6 / BAT.nominal_voltage_v ** 2
    assert BAT.nominal_voltage_v == pytest.approx(600.0, rel=1e-12)
    assert BAT.reaction_heat_coeff == pytest.approx(0.05, rel=1e-12)
    assert k == pytest.approx(0.0525, rel=1e-6), (
        f"产热系数 {k:.6f} W/kW² 偏离标定值 0.0525；温度与弦误差都会跟着动")


def test_heat_pwl_breakpoints_are_pinned():
    """SOS2 的 6 段断点：断点一动，弦的位置就动，热约束的保守性跟着动。"""
    p_pts, q_pts = heat_pwl_breakpoints(BAT, 6)
    assert [round(float(x), 4) for x in p_pts] == [
        0.0, 166.6667, 333.3333, 500.0, 666.6667, 833.3333, 1000.0]
    assert [round(float(x), 2) for x in q_pts] == [
        0.0, 1458.33, 5833.33, 13125.0, 23333.33, 36458.33, 52500.0], (
        "弦断点被改动。README 记载的最大偏差 364.58 W / 约 0.365 K 按这组断点算出")


# ------------------------------------------------------------------ #
#  寿命衰减模型
# ------------------------------------------------------------------ #
def test_degradation_coefficients_are_pinned():
    """四个 SOC 区间的衰减系数——README 明确列出的那张表。"""
    assert BAT.degradation_coeff == {
        "0.0-0.2": 3.0, "0.2-0.5": 1.5, "0.5-0.8": 1.0, "0.8-1.0": 2.0,
    }, "SOC 区间衰减系数表被改动（README 与前端图表都引用它）"


def test_cycle_life_and_cost_are_pinned():
    assert BAT.cycle_life == 6000, (
        "标称循环寿命被改动（80% DoD 下 6000 次）。"
        "注意代码里等效循环按 100% DoD 口径计算，两者口径差异见 battery_degradation_model.py")
    assert BAT.total_battery_cost == pytest.approx(1600000.0, rel=1e-12)


def test_degradation_is_conservative_vs_physical():
    """衰减成本必须**不低于**物理口径——这条性质是刻意保留的，不是巧合。

    背景（P2.4）：分子是 SOC 区间加权等效循环，分母是 80%DoD 标称寿命，
    两者口径不同源。审查意见曾建议"100%DoD 应是 4000 次，分母应改小"，
    实测证明方向相反：改小分母＝寿命更短＝每循环更贵＝保守度从 1.21
    升到 1.82 倍，更容易把赚钱的需求响应判成不划算。

    做法：取实际运行窗口 soc_min~soc_max，构造一个完整充放循环的 SOC 轨迹，
    比较本模型每循环成本与物理口径 `total_cost / L(DoD)`。
    """
    from src.models.battery_degradation_model import (
        BatteryDegradationModel,
        physical_lifetime_cycles,
    )

    dod = BAT.soc_max - BAT.soc_min          # 0.70，实际窗口
    assert dod == pytest.approx(0.70, abs=1e-9), "SOC 窗口被改动，下面的数值需重算"

    # 一个完整循环：从 soc_min 充到 soc_max 再放回 soc_min。
    # 注意**不要再除以 2**：模型内部已有 equivalent_cycles = Σcoeff·|ΔSOC| / 2，
    # 它把"充+放各半"折算成一次完整循环；这条轨迹正好是充一次放一次，
    # 所以 result.equivalent_cycles 就是 0.9520（每循环），成本直接就是每循环成本。
    down = np.linspace(BAT.soc_min, BAT.soc_max, 201)
    traj = np.concatenate([down, down[::-1]])
    model = BatteryDegradationModel(BAT)
    result = model.compute_degradation_from_soc_trajectory(traj)
    model_cost = result.degradation_cost_yuan

    assert result.equivalent_cycles == pytest.approx(0.9520, abs=1e-3), (
        f"一次完整循环计入 {result.equivalent_cycles:.4f} 个等效循环，"
        f"与标定值 0.9520 不符——模块 docstring 与 CHANGELOG 里引用了这个数")

    physical_cost = BAT.total_battery_cost / physical_lifetime_cycles(dod)

    ratio = model_cost / physical_cost
    assert ratio >= 1.0, (
        f"本模型每循环 {model_cost:.2f} 元 < 物理口径 {physical_cost:.2f} 元"
        f"（比值 {ratio:.3f}）——衰减成本变成了**低估**，这是危险方向："
        f"会把实际亏钱的充放电判成赚钱。若刚改过分子（去掉 SOC 区间加权）"
        f"或分母（6000→4000），请先读 battery_degradation_model.py 的模块 docstring")
    assert ratio == pytest.approx(1.213, abs=0.01), (
        f"保守度 {ratio:.3f} 偏离实测标定值 1.213（模型 {model_cost:.2f} 元/循环 vs "
        f"物理 {physical_cost:.2f} 元/循环）。这个数写进了模块 docstring 与 CHANGELOG，"
        f"改了就要同步改文档")


def test_battery_cost_per_kwh_matches_total_cost():
    """`battery_cost_per_kwh × 容量 == total_battery_cost`。

    补于 2026-10-12：这两个字段此前**没有任何测试约束**，且 `battery_cost_per_kwh`
    在计算链路里根本没有读者（全仓 3 处命中：定义、数据生成器的元数据导出、
    CSV 数据文件）。变异测试把 800 改成 880，快慢档全部指标一动不动 —— 一个
    看起来能调成本、实际不参与任何计算的旋钮，比没有这个旋钮更危险。
    """
    implied = BAT.battery_cost_per_kwh * BAT.rated_capacity_kwh
    assert implied == pytest.approx(BAT.total_battery_cost, rel=1e-9), (
        f"电池单价 {BAT.battery_cost_per_kwh} 元/kWh × 容量 {BAT.rated_capacity_kwh} kWh "
        f"= {implied:.0f} 元，与 total_battery_cost={BAT.total_battery_cost:.0f} 元不一致。"
        f"衰减计费实际读的是 total_battery_cost（unit_cost = 总成本/(2·容量·循环寿命)），"
        f"两个字段对不上时，改单价不会影响任何结果")


def test_degradation_unit_cost_is_pinned():
    """unit_cost = total_cost / (2·cap·cycle_life)，元 per 加权 kWh。

    它就是目标函数里那一项的系数；README 的衰减 388.37 元由它线性放大。
    """
    unit_cost = BAT.total_battery_cost / (2.0 * BAT.rated_capacity_kwh * BAT.cycle_life)
    assert unit_cost == pytest.approx(0.0666667, rel=1e-6), (
        f"衰减单位成本 {unit_cost:.8f} 元/加权kWh 偏离标定值 0.0666667")


# ------------------------------------------------------------------ #
#  电价口径（331 号文比例关系）
# ------------------------------------------------------------------ #
def test_price_ratios_follow_the_document():
    """峰:平:谷 = 1.7:1:0.38，尖峰在高峰基础上浮 25%。

    这四个数**不是手填的**，是由平段基准价按固定比例推导。手改任一个都会
    破坏比例关系，而比例关系才是"引用粤发改价格〔2021〕331 号"的实质内容。
    """
    assert PRICE.flat_price == pytest.approx(0.65, rel=1e-12), (
        "平段基准价被改动（示例值，非某户实际到户价）")
    assert PRICE.peak_price == pytest.approx(PRICE.flat_price * 1.7, rel=1e-9), (
        "高峰价不再等于平段 × 1.7，331 号文的比例关系被破坏")
    assert PRICE.valley_price == pytest.approx(PRICE.flat_price * 0.38, rel=1e-9), (
        "低谷价不再等于平段 × 0.38")
    assert PRICE.spike_price == pytest.approx(PRICE.peak_price * 1.25, rel=1e-9), (
        "尖峰价不再等于高峰 × 1.25（1.38125 刻意不取整就是为了保住这个比例）")


def test_price_absolute_values_are_pinned():
    """绝对值的第二道锁：比例对但基准整体挪动时，上一条查不出来。"""
    assert PRICE.spike_price == pytest.approx(1.38125, rel=1e-9)
    assert PRICE.peak_price == pytest.approx(1.105, rel=1e-9)
    assert PRICE.valley_price == pytest.approx(0.247, rel=1e-9)


# ------------------------------------------------------------------ #
#  时间粒度
# ------------------------------------------------------------------ #
def test_time_discretization_is_pinned():
    """96 点 / 15 分钟。改成 48 或 24 会让峰值温度被系统性低估（README 护栏 3）。"""
    assert BAT.num_steps == 96, (
        "时间粒度被改动。粗分辨率会系统性低估峰值温度：同一条曲线 24 点 34.33 ℃、"
        "48 点 39.75 ℃、96 点 44.73 ℃，差 10.4 ℃——热约束会静默失效")
    assert BAT.time_step_hours == pytest.approx(0.25, rel=1e-12)
    assert BAT.num_steps * BAT.time_step_hours == pytest.approx(24.0, rel=1e-12), (
        "96 × 0.25 必须正好是一天，否则日循环约束会漏掉或重复时段")
