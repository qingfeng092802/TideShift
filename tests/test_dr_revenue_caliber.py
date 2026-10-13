# -*- coding: utf-8 -*-
"""回归测试：DR 收益校验必须与衰减模型同口径（P2.3）。

历史缺陷：同一个"寿命衰减"在仓里有三套互不相干的算法——

1. MILP 目标函数（`storage_optimization_agent.py` 的 `unit_cost` 常数
   `total_cost / (2 × Cap × cycle_life)` + SOC 区间加权）；
2. 后验报表（`battery_degradation_model.compute_degradation_from_power`
   对整条 SOC 轨迹做中点加权）；
3. DR 判定（`demand_response_agent._check_dr_revenue`）——**它自己一套**：
   `target_reduction_kw × 时长 × 0.8`（拍脑袋的响应率）算出响应电量，
   再乘硬编码的 `1.5`（本意是 SOC 0.2-0.5 区间的衰减系数，却没有按真实
   SOC 取值）当等效循环增量。

危害不是数值大小，而是**判定依据不在优化器的视野里**：优化器按第 1 套
认为响应划算并据此排产，DR 判定却按第 3 套算出一个不同的净收益，两者
不一致时用户看到的是"优化器排了响应、DR 页却说拒绝"。

本文件守两件事：
- 收益校验的衰减成本 == 同一模型对"基础调度 / 响应后调度"两条轨迹的差分；
- 收益字典的键名与类型保持稳定（前端与缓存序列化都依赖它）。
"""
import json

import numpy as np
import pandas as pd
import pytest

from src.agents.demand_response_agent import DemandResponseAgent, DRSignal
from src.models.battery_degradation_model import BatteryDegradationModel
from src.utils.config import CONFIG


@pytest.fixture
def agent():
    return DemandResponseAgent(CONFIG)


def _day_index():
    return pd.date_range("2024-07-30 00:00", periods=96, freq="15min")


def _soc_feasible_schedule():
    """一条 SOC 全程留在 [soc_min, soc_max] 内的基础调度。

    必须满足这个前提：`compute_degradation_from_power` 会把越界的 SOC
    夹回 `[soc_min, soc_max]`，若基础调度本来就贴着下限，基础与响应后
    两条轨迹会被夹成同一条，差分恒为 0 —— 那样这条用例就测不到东西了。
    真实 MILP 输出天然满足 SOC 约束，这里手工构造一条等效的：
    容量 2000kWh、η=0.95、dt=0.25h，充电 200kW → +0.02632/步，
    放电 100kW → -0.01316/步。
    """
    base = np.zeros(96)
    base[0:20] = -200.0   # 00:00-05:00 充电
    base[56:76] = 100.0   # 14:00-19:00 放电
    return base


def _dr_signal():
    return DRSignal(
        start_time="2024-07-30 15:00", end_time="2024-07-30 17:00",
        target_reduction_kw=400.0, subsidy_per_kwh=0.8,
    )


def test_revenue_uses_same_model_as_report(agent):
    """收益校验的衰减成本必须等于模型对两条轨迹的差分，误差为 0。"""
    idx = _day_index()
    base = _soc_feasible_schedule()
    dr = _dr_signal()
    dr_indices = np.where(
        (idx >= pd.to_datetime(dr.start_time)) & (idx < pd.to_datetime(dr.end_time))
    )[0]

    adjusted = base.copy()
    for i in dr_indices:
        adjusted[i] = min(adjusted[i] + 400.0, CONFIG.battery.rated_power_kw)

    check = agent._check_dr_revenue(dr_indices, dr, base, adjusted, 0.5)

    model = BatteryDegradationModel(CONFIG.battery)
    base_deg = model.compute_degradation_from_power(base, initial_soc=0.5)
    adj_deg = model.compute_degradation_from_power(adjusted, initial_soc=0.5)
    expected = adj_deg.degradation_cost_yuan - base_deg.degradation_cost_yuan

    assert check["estimated_degradation"] == pytest.approx(round(expected, 2), abs=1e-9), (
        f"DR 判定算出的额外衰减 {check['estimated_degradation']} 元与衰减模型的差分 "
        f"{expected:.2f} 元不一致——说明 DR 判定又回到了自己那一套常数估算。"
        f"（比对的是 round(…, 2)：该字段是给前端展示用的两位小数，"
        f"不是模型的原始精度）")
    assert expected > 0, (
        "本用例的前提是响应确实增加了衰减；若为 0 说明两条 SOC 轨迹被 "
        "soc_min/soc_max 夹成了同一条，用例本身失效")


def test_revenue_does_not_use_hardcoded_response_rate(agent):
    """旧实现里的 0.8（响应率）与 1.5（区间系数）不得再影响结果。

    做法：把目标削减功率翻倍但保持 `adjusted_power` 不变——旧实现完全按
    `target_reduction_kw` 估算，翻倍会让结果大变；新实现只看真实功率差分，
    目标功率不再进入衰减计算，结果必须**完全相同**。
    """
    idx = _day_index()
    base = _soc_feasible_schedule()
    dr_indices = np.where(
        (idx >= pd.to_datetime("2024-07-30 15:00"))
        & (idx < pd.to_datetime("2024-07-30 17:00"))
    )[0]

    adjusted = base.copy()
    for i in dr_indices:
        adjusted[i] = min(adjusted[i] + 400.0, CONFIG.battery.rated_power_kw)

    low = agent._check_dr_revenue(dr_indices, _dr_signal(), base, adjusted, 0.5)

    big = DRSignal(
        start_time="2024-07-30 15:00", end_time="2024-07-30 17:00",
        target_reduction_kw=800.0, subsidy_per_kwh=0.8,
    )
    high = agent._check_dr_revenue(dr_indices, big, base, adjusted, 0.5)

    assert low["estimated_degradation"] == pytest.approx(
        high["estimated_degradation"], abs=1e-9), (
        "目标削减功率改变了衰减成本——衰减只应取决于真实的功率轨迹差分，"
        "目标功率是「想响应多少」，不是「实际响应了多少」")
    # 补贴仍应按真实响应电量算，故两者相同；但若响应电量被目标功率带偏就会不等
    assert low["estimated_subsidy"] == pytest.approx(high["estimated_subsidy"], abs=1e-9)


def test_revenue_dict_keys_are_stable(agent):
    """键名是外部契约（前端展示 + `feasibility_check` 序列化），不许改名。"""
    idx = _day_index()
    base = _soc_feasible_schedule()
    dr = _dr_signal()
    dr_indices = np.where(
        (idx >= pd.to_datetime(dr.start_time)) & (idx < pd.to_datetime(dr.end_time))
    )[0]
    adjusted = base.copy()
    for i in dr_indices:
        adjusted[i] = min(adjusted[i] + 400.0, CONFIG.battery.rated_power_kw)

    check = agent._check_dr_revenue(dr_indices, dr, base, adjusted, 0.5)
    for key in ("profitable", "estimated_subsidy", "estimated_degradation", "net_revenue"):
        assert key in check, f"收益校验结果缺少契约键 {key}"


def test_revenue_values_are_json_native(agent):
    """`feasibility_check` 会随 `DRResponse` 一起被缓存序列化并出现在接口里。

    模型返回的是 `np.float64`，而 `np.bool_` 不是 `bool` 的子类——
    不显式转换的话 `json.dumps` 会抛 TypeError，且失败点离病根很远。
    """
    idx = _day_index()
    base = _soc_feasible_schedule()
    dr = _dr_signal()
    dr_indices = np.where(
        (idx >= pd.to_datetime(dr.start_time)) & (idx < pd.to_datetime(dr.end_time))
    )[0]
    adjusted = base.copy()
    for i in dr_indices:
        adjusted[i] = min(adjusted[i] + 400.0, CONFIG.battery.rated_power_kw)

    check = agent._check_dr_revenue(dr_indices, dr, base, adjusted, 0.5)

    assert type(check["profitable"]) is bool, (
        f"profitable 的类型是 {type(check['profitable']).__name__}，"
        f"应为内置 bool（np.bool_ 不是 bool 的子类，json.dumps 会拒绝）")
    for key in ("estimated_subsidy", "estimated_degradation", "net_revenue"):
        assert type(check[key]) is float, (
            f"{key} 的类型是 {type(check[key]).__name__}，应为内置 float")
    # 端到端确认：整个字典能直接 JSON 序列化
    json.dumps(check)


def test_net_revenue_identity_closes(agent):
    """净收益 == 补贴 − 额外衰减（四舍五入后允许 0.02 元的展示误差）。"""
    idx = _day_index()
    base = _soc_feasible_schedule()
    dr = _dr_signal()
    dr_indices = np.where(
        (idx >= pd.to_datetime(dr.start_time)) & (idx < pd.to_datetime(dr.end_time))
    )[0]
    adjusted = base.copy()
    for i in dr_indices:
        adjusted[i] = min(adjusted[i] + 400.0, CONFIG.battery.rated_power_kw)

    check = agent._check_dr_revenue(dr_indices, dr, base, adjusted, 0.5)
    assert check["net_revenue"] == pytest.approx(
        check["estimated_subsidy"] - check["estimated_degradation"], abs=0.02)


def test_zero_response_is_not_profitable(agent):
    """一度电都没多放时必须判为不划算——这是旧实现最隐蔽的一个缺陷。

    复现场景（2024-07-30 11:00-12:00，target 500kW）：基础调度在那个小时
    **已经贴着额定功率 1000kW 放电**，于是
    `adjusted_power = min(base + max_response_power, rated_power_kw)`
    加不上去，`adjusted_power == base_power`。旧实现只看
    `target_reduction_kw`（被热安全降额成 707kW）与补贴单价，估出
    "补贴 565.6 元、衰减 56.56 元、净赚 509.04 元"，
    返回 `accepted=True` 而 `response_power_kw=0.0` —— **对外宣称接受响应、
    实际 0kW**，同时把 `dr_events_count` 从 1 涨到 2。
    它不改任何已发布的汇总数字（补贴、净收益都不变），所以长期无人发现。
    """
    idx = _day_index()
    base = _soc_feasible_schedule()
    dr = _dr_signal()
    dr_indices = np.where(
        (idx >= pd.to_datetime(dr.start_time)) & (idx < pd.to_datetime(dr.end_time))
    )[0]

    # 零响应：响应后调度与基础调度逐点相同
    check = agent._check_dr_revenue(dr_indices, dr, base, base.copy(), 0.5)

    assert check["response_energy_kwh"] == pytest.approx(0.0, abs=1e-9), (
        "零响应场景下响应电量应为 0")
    assert check["estimated_subsidy"] == pytest.approx(0.0, abs=1e-9), (
        "没有真实响应电量就不该有补贴收入")
    assert check["profitable"] is False, (
        f"零响应却判为划算（净收益 {check['net_revenue']} 元）——说明判定又绕开了"
        f"真实功率轨迹，只看目标功率在估算。这种『假接受』会让 DR 页显示"
        f"已响应、实际功率曲线纹丝不动")


def test_no_response_headroom_is_detected_by_target_power_alone(agent):
    """目标功率再大也不能凭空造出响应——钉住"实际功率是唯一依据"。

    与上一条的区别：这里把目标功率放大 100 倍，零响应仍然是零响应。
    旧实现会随目标功率线性放大估算收益，从而在完全相同的物理事实上
    给出完全相反的判定。
    """
    idx = _day_index()
    base = _soc_feasible_schedule()
    dr_indices = np.where(
        (idx >= pd.to_datetime("2024-07-30 15:00"))
        & (idx < pd.to_datetime("2024-07-30 17:00"))
    )[0]

    huge = DRSignal(
        start_time="2024-07-30 15:00", end_time="2024-07-30 17:00",
        target_reduction_kw=40000.0, subsidy_per_kwh=10.0,
    )
    check = agent._check_dr_revenue(dr_indices, huge, base, base.copy(), 0.5)

    assert check["profitable"] is False, (
        "把 target_reduction_kw 与 subsidy_per_kwh 放大后，零响应被判成了划算——"
        "判定依据里仍混有目标功率，而目标功率不是实际执行结果")
