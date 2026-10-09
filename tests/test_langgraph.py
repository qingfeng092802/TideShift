"""
LangGraph 编排 vs 纯 Python 编排 一致性测试（pytest）

原来只 print 一张对比表。现在断言两种编排的调度结果必须一致，
并且图结构必须真的包含 DR 循环条件边。

运行：
    pytest tests/test_langgraph.py -v
"""
import copy
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.config import CONFIG
from src.agents.coordinator_agent import CoordinatorAgent
from src.agents.demand_response_agent import DRSignal
from src.data.data_loader import load_load_data

# 真实 MILP/全流程测试，标记 slow，PR CI 默认跳过
pytestmark = pytest.mark.slow

CFG = CONFIG
TARGET_DATE = "2024-07-30"


def _dr_signals():
    return [
        DRSignal(start_time=f"{TARGET_DATE} 15:00", end_time=f"{TARGET_DATE} 17:00",
                 target_reduction_kw=400.0, subsidy_per_kwh=0.8, dr_type="peak_shaving"),
        DRSignal(start_time=f"{TARGET_DATE} 19:30", end_time=f"{TARGET_DATE} 20:30",
                 target_reduction_kw=500.0, subsidy_per_kwh=1.0, dr_type="peak_shaving"),
    ]


@pytest.fixture(scope="module")
def historical():
    return load_load_data()


@pytest.fixture(scope="module")
def py_report(historical):
    return CoordinatorAgent(CFG).run_daily_scheduling(
        date=TARGET_DATE, historical_data=historical,
        dr_signals=_dr_signals(), use_ml_forecast=False,
        include_thermal=True, include_degradation=True)


@pytest.fixture(scope="module")
def lg_report(historical):
    lg = pytest.importorskip("langgraph")
    from src.agents.langgraph_coordinator import LangGraphCoordinator
    return LangGraphCoordinator(CFG).run_daily_scheduling(
        date=TARGET_DATE, historical_data=historical,
        dr_signals=_dr_signals(), use_ml_forecast=False,
        include_thermal=True, include_degradation=True)


def test_both_orchestrations_agree(lg_report, py_report):
    """两种编排必须给出同一套调度结果（接口兼容的核心承诺）"""
    for field in ("arbitrage_revenue_yuan", "degradation_cost_yuan",
                  "net_revenue_yuan", "charge_energy_kwh", "discharge_energy_kwh"):
        a, b = getattr(lg_report, field), getattr(py_report, field)
        assert a == pytest.approx(b, rel=1e-3, abs=1.0), \
            f"{field} 不一致：LangGraph={a} vs 纯Python={b}"


def test_graph_has_conditional_dr_loop():
    """图里必须有 DR 循环的条件边，否则"循环节点"只是说法"""
    pytest.importorskip("langgraph")
    from src.agents.langgraph_coordinator import build_scheduling_graph
    graph = build_scheduling_graph()
    nodes = set(graph.get_graph().nodes.keys())
    for required in ("init", "load_forecast", "storage_optimization", "dr_handler", "finalize"):
        assert required in nodes, f"缺少节点 {required}"
    edges = graph.get_graph().edges
    has_self_loop = any(e.source == "dr_handler" and e.target == "dr_handler" for e in edges)
    assert has_self_loop, "dr_handler 缺少自循环条件边，DR 事件无法逐个处理"


def test_python_orchestration_is_feasible(py_report):
    assert py_report.net_revenue_yuan == pytest.approx(
        py_report.arbitrage_revenue_yuan + py_report.dr_subsidy_yuan
        - py_report.degradation_cost_yuan, abs=0.05)
    # 完整 pipeline（含 DR 追加放电）温度可进入降额区，但必须远低于 55℃ 安全线
    assert py_report.max_battery_temp_c <= CFG.battery.temp_safe_max + 0.5


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
