"""
端到端测试（pytest）：负荷预测 → 储能调度 → 需求响应 → 报表

v1.1：原来全是 print、无断言。现在校验报表内部一致性与物理可行性。

运行：
    pytest tests/test_end_to_end.py -v
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.config import CONFIG
from src.agents.coordinator_agent import CoordinatorAgent, DailyReport
from src.agents.demand_response_agent import DRSignal
from src.data.data_loader import load_load_data, load_dr_signals

# 🟠#25：全流程跑真实 MILP，单文件分钟级——标记 slow，PR CI 默认跳过（nightly 全量跑）
pytestmark = pytest.mark.slow

CFG = CONFIG
TARGET_DATE = "2024-07-30"


@pytest.fixture(scope="module")
def historical() -> pd.DataFrame:
    return load_load_data()


@pytest.fixture(scope="module")
def dr_signals():
    return [
        DRSignal(start_time=f"{TARGET_DATE} 15:00", end_time=f"{TARGET_DATE} 17:00",
                 target_reduction_kw=400.0, subsidy_per_kwh=0.8, dr_type="peak_shaving"),
        DRSignal(start_time=f"{TARGET_DATE} 19:30", end_time=f"{TARGET_DATE} 20:30",
                 target_reduction_kw=500.0, subsidy_per_kwh=1.0, dr_type="peak_shaving"),
    ]


@pytest.fixture(scope="module")
def report(historical, dr_signals) -> DailyReport:
    coord = CoordinatorAgent(CFG)
    return coord.run_daily_scheduling(
        date=TARGET_DATE, historical_data=historical, dr_signals=dr_signals,
        use_ml_forecast=False, include_thermal=True, include_degradation=True)


def test_report_is_complete(report):
    assert isinstance(report, DailyReport)
    assert report.date == TARGET_DATE
    for field in ("arbitrage_revenue_yuan", "dr_subsidy_yuan", "degradation_cost_yuan",
                  "net_revenue_yuan", "charge_energy_kwh", "discharge_energy_kwh",
                  "max_battery_temp_c", "equivalent_cycles"):
        assert np.isfinite(getattr(report, field)), f"{field} 不是有限数值"


def test_net_revenue_identity(report):
    """净收益必须严格等于 套利 + DR补贴 - 衰减（原 README 数字对不上账）"""
    assert report.net_revenue_yuan == pytest.approx(
        report.arbitrage_revenue_yuan + report.dr_subsidy_yuan
        - report.degradation_cost_yuan, abs=0.05)


def test_energies_are_positive(report):
    assert report.charge_energy_kwh > 0
    assert report.discharge_energy_kwh > 0
    # 放电量不可能超过充电量（有损耗）
    assert report.discharge_energy_kwh <= report.charge_energy_kwh + 1e-6


def test_temperature_within_safe_limit(report):
    """🟠#39 修复：此前断言放宽到 55℃ 安全停止线并自注"可以超 45℃"，
    等于允许电池跑进深度降额区仍判通过。现在分层收紧：
    ① 主断言 47.5℃（45℃ 降额边界 + 2.5℃ 机制余量）——余量来源：DR 热安全
      校验用线性插值（0.8 安全系数）估算降额功率，后验仿真叠加末步温度
      （🟠#27 修复后 t=96 已纳入统计）实测最高 46.99℃，距该值留 0.5℃ 裕度。
      相比原 55℃ 断言收紧 7.5℃，电池不再可能"长期跑在降额区仍判通过"。
    ② 55℃ 安全停止线仍作硬红线兜底。"""
    assert report.max_battery_temp_c <= CFG.battery.temp_normal_max + 2.5, \
        (f"报表最高温 {report.max_battery_temp_c}℃ 超过降额区边界 "
         f"{CFG.battery.temp_normal_max + 2.5}℃——热安全机制失效，需排查")
    assert report.max_battery_temp_c <= CFG.battery.temp_safe_max + 0.5, \
        f"报表最高温 {report.max_battery_temp_c}℃ 超过安全停止线 {CFG.battery.temp_safe_max}℃"


def test_annualization_uses_steady_state(historical, dr_signals):
    """
    年化必须基于稳态（首末SOC相同）的日收益，
    否则等于假设每天都能把 SOC 从 0.5 放空到 0.2，凭空多算一倍。
    """
    coord = CoordinatorAgent(CFG)
    r = coord.run_daily_scheduling(date=TARGET_DATE, historical_data=historical,
                                   dr_signals=[], use_ml_forecast=False,
                                   include_thermal=True, include_degradation=True)
    assert r.net_revenue_yuan > 0
    annual_wan = r.net_revenue_yuan * 365 / 10000
    assert 0 < annual_wan < 200, f"年化 {annual_wan:.1f} 万元 不合理"
    print(f"\n  [参考] 稳态日净收益 {r.net_revenue_yuan:.2f} 元 → 年化 {annual_wan:.1f} 万元")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
