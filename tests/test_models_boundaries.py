# -*- coding: utf-8 -*-
"""数值边界测试（衰减模型除零 / NaN SOC / 热模型末点统计）

此前 battery_degradation_model.estimate_degradation_cost 在 soc_range 上下限相等时
除零、SOC 含 NaN 时行为未定义，均零测试覆盖。
"""
import numpy as np
import pytest

from src.utils.config import CONFIG
from src.models.battery_degradation_model import BatteryDegradationModel
from src.models.battery_thermal_model import BatteryThermalModel, estimate_temperature_rise


def test_estimate_degradation_zero_dod_raises():
    """soc_range 上下限相等（DoD=0）必须显式报错而非除零/inf。"""
    m = BatteryDegradationModel(CONFIG.battery)
    with pytest.raises(ValueError):
        m.estimate_degradation_cost(100.0, 100.0, soc_range=(0.5, 0.5))


def test_degradation_nan_soc_raises():
    """SOC 轨迹含 NaN 必须显式报错。"""
    m = BatteryDegradationModel(CONFIG.battery)
    soc = np.linspace(0.5, 0.9, 20)
    soc[5] = np.nan
    with pytest.raises(ValueError):
        m.compute_degradation_from_soc_trajectory(soc)


def test_degradation_clip_uses_config_bounds():
    """衰减模型重放 SOC 按 soc_min/soc_max 裁剪（而非 0~1）。"""
    m = BatteryDegradationModel(CONFIG.battery)
    huge_discharge = np.full(96, 5000.0)  # 远超容量，SOC 会跌破下限
    res = m.compute_degradation_from_power(huge_discharge, initial_soc=0.5)
    assert res.soc_trajectory.min() >= m.cfg.soc_min - 1e-9


def test_thermal_max_includes_final_step():
    """末步结束温度必须纳入最高温统计。

    构造持续升温序列：若末点温度不进统计，max_temperature_c 将系统性偏低。
    """
    th = BatteryThermalModel(CONFIG.battery)
    power = np.full(96, 1000.0)  # 满功率放电，温度单调上升
    amb = np.full(96, 30.0)
    out = th.simulate_full_day(power, amb, initial_temp=30.0)
    assert out["max_temperature_c"] >= out["temperature_c"][-1] - 1e-6


def test_estimate_temperature_rise_charge_sign():
    """estimate_temperature_rise 充电/放电符号必须与 compute_heat_generation 自洽。

    充电（负功率）的反应热为 -0.5·coeff·joule → 同等 |P| 下充电稳态温升应低于放电。
    """
    t_dis = estimate_temperature_rise(800, 1.0, ambient_temp=30.0, initial_temp=30.0)
    t_chg = estimate_temperature_rise(-800, 1.0, ambient_temp=30.0, initial_temp=30.0)
    assert t_chg < t_dis, f"充电稳态温升({t_chg:.2f})应低于放电({t_dis:.2f})（符号不自洽）"
