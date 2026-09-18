# -*- coding: utf-8 -*-
"""回归测试：DR 热安全校验必须覆盖窗口后热惯性过冲。

历史缺陷：_check_thermal_safety 只取 DR 窗口内温度最大值，
窗口结束后（基础调度继续大功率放电）的热惯性过冲漏检，
过冲曾实测到 DR 后 48.9℃ 越过 45℃ 降额线，故本用例守这条边界。
"""
import numpy as np
import pandas as pd
import pytest

from src.agents.demand_response_agent import DemandResponseAgent
from src.utils.config import CONFIG


@pytest.fixture
def agent():
    return DemandResponseAgent(CONFIG)


def _day_index():
    return pd.date_range("2024-07-15 00:00", periods=96, freq="15min")


def test_window_overshoot_detected(agent):
    """窗口内温度 <45℃，但窗口后基础调度继续大功率放电 → 全天峰值必须被检出。"""
    idx = _day_index()
    base_power = np.zeros(96)
    # DR 窗口 10:00-11:00，基础功率很小，目标削减 100kW（温和响应）
    dr_start, dr_end = idx[40], idx[44]
    dr_indices = np.where((idx >= dr_start) & (idx < dr_end))[0]
    base_power[dr_indices] = 100.0
    # 窗口后 11:00-15:00 基础调度大功率放电（不带 DR 标签）→ 热惯性持续攀升
    base_power[44:60] = 1000.0

    ambient = np.full(96, 30.0)
    check = agent._check_thermal_safety(
        dr_indices, base_power, current_soc=0.5, current_temp=30.0,
        ambient_temp=ambient, target_reduction=100.0,
    )

    assert check["window_max_temp"] < 45.0, "窗口内应低于45℃（旧实现只会看到这个）"
    assert check["estimated_max_temp"] >= 45.0, "全天峰值应达到/超过45℃（过冲被检出）"
    assert check["estimated_max_temp"] > check["window_max_temp"], "全天峰值应高于窗口内峰值"
    assert check["safe"] is False, "过冲场景必须判定为不安全"
    assert check["overshoot_note"], "应给出过冲说明"


def test_normal_dr_still_passes(agent):
    """温和响应且窗口外无大功率：不应被新校验误伤。"""
    idx = _day_index()
    base_power = np.zeros(96)
    dr_start, dr_end = idx[40], idx[44]
    dr_indices = np.where((idx >= dr_start) & (idx < dr_end))[0]
    base_power[dr_indices] = 100.0

    ambient = np.full(96, 30.0)
    check = agent._check_thermal_safety(
        dr_indices, base_power, current_soc=0.5, current_temp=30.0,
        ambient_temp=ambient, target_reduction=100.0,
    )
    assert check["safe"] is True
    assert check["overshoot_note"] == ""
