"""
回归 README「量化成果表」的 5 个 headline 数字。

失败语义：求解器小版本 / 数值内核 / 模型口径三者之一变了。
这不是"用观测值当随机阈值"——本测试与 `test_end_to_end.py:88-103`
拒绝的那类断言不是同一类，因为：
- 它跑的是 README 承诺的完整复现路径（`use_ml_forecast=True`）
- 数值本身可被任何 clone 的人用 45 秒独立复算（`docs/experiments.md` §复现脚本）
- 失败时排查方向明确（三者之一），不是玄学
"""
# 补于 2026-10-04：此前 README 五个 headline 数字在 tests/ 里命中 0，
# 意味着改了物理常数 / 离散步长 / SOC 权重系数都会静默变错。
# 本测试把它们钉住。复现路径与口径见 docs/experiments.md §复现脚本。
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.config import CONFIG
from src.agents.coordinator_agent import CoordinatorAgent, DailyReport
from src.agents.demand_response_agent import DRSignal
from src.data.data_loader import load_load_data

# 真实整轮 MILP（含 XGBoost 训练），单轮分钟级以内——PR CI 默认跳过，
# nightly 与 workflow_dispatch 全量跑。这是刻意的：钉绝对值的代价就是每次
# 都要真跑一遍，把它放进 PR 只会让人为了省时间而删掉它。
pytestmark = pytest.mark.slow

CFG = CONFIG
TARGET_DATE = "2024-07-30"

# 期望值与容差**照抄** `docs/experiments.md` §复现脚本里那四条 `assert`
# （±0.5 / ±0.5 / ±0.2 / ±0.01），不在这里另立一套口径。
# 动了电价日历、热模型或衰减权重之后要更新的是**实验文档那一处**，这里跟着走。
# ⚠️ 两个 DR 事件必须与那段脚本逐字一致，第二个窗口是 **11:00-12:00**；
# `tests/test_end_to_end.py:49-50` 用的是 19:30-20:30。这是**有意的不同**：
# 那条测恒等式与机制是否生效，不依赖具体解；这条钉 README 那一列的绝对值。
# 若把它们"顺手统一"，本文件四条绝对值断言会立刻变红——那是口径漂移，不是回归。
DR_SIGNALS = [
    DRSignal(start_time=f"{TARGET_DATE} 15:00", end_time=f"{TARGET_DATE} 17:00",
             target_reduction_kw=400.0, subsidy_per_kwh=0.8, dr_type="peak_shaving"),
    DRSignal(start_time=f"{TARGET_DATE} 11:00", end_time=f"{TARGET_DATE} 12:00",
             target_reduction_kw=500.0, subsidy_per_kwh=1.0, dr_type="peak_shaving"),
]


@pytest.fixture(scope="module")
def report() -> DailyReport:
    """整轮只跑一次，本文件所有用例共用（一次约 40~60 s）。"""
    return CoordinatorAgent(CFG).run_daily_scheduling(
        date=TARGET_DATE, historical_data=load_load_data(), dr_signals=DR_SIGNALS,
        use_ml_forecast=True,      # ← README 复现路径的口径（test_end_to_end 走的是 False）
        include_thermal=True, include_degradation=True)


def test_arbitrage_revenue_matches_readme(report):
    assert report.arbitrage_revenue_yuan == pytest.approx(1816.27, abs=0.5), (
        f"套利 {report.arbitrage_revenue_yuan:.2f} 元 ≠ README 量化成果表的 1816.27（容差 ±0.5）。"
        f"先查三处：电价日历（331 号文口径）、求解器版本（pulp/highspy）、"
        f"目标函数口径。复算入口：docs/experiments.md §复现脚本")


def test_degradation_cost_matches_readme(report):
    assert report.degradation_cost_yuan == pytest.approx(388.37, abs=0.5), (
        f"后验衰减 {report.degradation_cost_yuan:.2f} 元 ≠ README 的 388.37（容差 ±0.5）。"
        f"这条对衰减系数表、SOC 区间划分与 unit_cost 三项最敏感；"
        f"注意它与模型内那套账本来就不同源（见 README 已知局限）")


def test_max_battery_temp_matches_readme(report):
    assert report.max_battery_temp_c == pytest.approx(46.92, abs=0.2), (
        f"后验最高温 {report.max_battery_temp_c:.2f} ℃ ≠ README 的 46.92（容差 ±0.2）。"
        f"46.92 是「MILP 内逐步硬约束 + 后验 RC 仿真全天峰值」两段的合成结果，"
        f"热容/热阻/产热系数/步长任一处变化都会动它")


def test_equivalent_cycles_match_readme(report):
    assert report.equivalent_cycles == pytest.approx(1.4564, abs=0.01), (
        f"等效循环 {report.equivalent_cycles:.4f} 次 ≠ README 的 1.4564（容差 ±0.01）。"
        f"这条最接近「吞吐」本身：SOC 窗口、功率上限或互斥约束变化都会反映在这里")


def test_net_revenue_is_derived_not_pinned(report):
    """净收益 1587.74 是**派生量**，不钉绝对值。

    钉了就是把同一个数断两遍：它不会指出任何新问题，却会在求解器换版时和上面
    四条一起红（同一根因、四倍噪音）。这里只要求它按恒等式闭合——上面三项守住，
    它自然落在 README 那一格（1587.74 = 1816.27 − 388.37 + 159.84）。
    """
    assert report.net_revenue_yuan == pytest.approx(
        report.arbitrage_revenue_yuan + report.dr_subsidy_yuan - report.degradation_cost_yuan,
        abs=0.05), (
        f"净收益 {report.net_revenue_yuan:.2f} ≠ 套利 {report.arbitrage_revenue_yuan:.2f} "
        f"+ DR补贴 {report.dr_subsidy_yuan:.2f} − 衰减 {report.degradation_cost_yuan:.2f}："
        f"报表内部口径断了，这一条与求解器版本无关，优先查记账逻辑")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
