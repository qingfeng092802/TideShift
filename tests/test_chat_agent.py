"""
对话Agent测试（pytest，规则模式，无需 API Key）

v1.1：原来是脚本式 print。现在断言工具函数真的返回了内容、且数字与调度结果一致。

运行：
    pytest tests/test_chat_agent.py -v
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.config import CONFIG
from src.agents.chat_agent import create_agent, AgentContext, SchedulingTools
from src.agents.coordinator_agent import CoordinatorAgent
from src.agents.demand_response_agent import DRSignal
from src.data.data_loader import load_load_data

DATE = "2024-07-30"


@pytest.fixture(scope="module")
def tools():
    df = load_load_data()
    dr_signals = [
        DRSignal(start_time=f"{DATE} 15:00", end_time=f"{DATE} 17:00",
                 target_reduction_kw=400, subsidy_per_kwh=0.8, dr_type="peak_shaving"),
    ]
    coord = CoordinatorAgent(CONFIG)
    report = coord.run_daily_scheduling(date=DATE, historical_data=df,
                                        dr_signals=dr_signals, use_ml_forecast=False,
                                        include_thermal=True, include_degradation=True)
    # 基准：同一天的 baseline_strategy 结果（让对比工具返回有内容）
    from src.agents.storage_optimization_agent import StorageOptimizationAgent
    import pandas as _pd, numpy as _np
    from src.data.data_generator import generate_price_profile, generate_ambient_temp
    _tidx = _pd.date_range(DATE, periods=96, freq="15min")
    _price = _np.asarray(generate_price_profile(_tidx), dtype=float)
    _amb = _np.asarray(generate_ambient_temp(_tidx), dtype=float)
    baseline = StorageOptimizationAgent(CONFIG).baseline_strategy(_price, _amb)
    viz_data = coord.get_visualization_data() if hasattr(coord, "get_visualization_data") else None
    ctx = AgentContext(report=report, coordinator=coord, baseline=baseline,
                       viz_data=viz_data, dr_signals=dr_signals)
    return SchedulingTools(ctx), report


def test_every_tool_returns_content(tools):
    t, _ = tools
    for name, call in [
        ("get_report", lambda: t.get_report()),
        ("compare_baseline", lambda: t.compare_baseline()),
        ("explain_schedule", lambda: t.explain_schedule(14)),
        ("get_thermal_info", lambda: t.get_thermal_info()),
        ("get_dr_info", lambda: t.get_dr_info()),
    ]:
        out = call()
        assert isinstance(out, str) and len(out.strip()) > 10, f"{name} 返回内容为空"


def test_rule_agent_answers_basic_questions(tools):
    """🟠#40 修复：不再只断言"非空"——同时校验回答内容的正确性
    （收益问题必须带与报表一致的数字、温度问题必须带温度值/安全结论）。"""
    t, report = tools
    # create_agent 第一个参数是 AgentContext（内部再创建 SchedulingTools）
    agent = create_agent(t.ctx, api_key="")
    resp_revenue = agent.respond("今天的收益是多少")
    assert isinstance(resp_revenue, str) and len(resp_revenue.strip()) > 0, "收益问题没有回答"
    nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.\d+", resp_revenue)]
    assert any(abs(n - report.net_revenue_yuan) < 1.0 for n in nums), \
        f"收益回答未包含与报表一致的净收益 {report.net_revenue_yuan}：{resp_revenue[:120]}"
    resp_temp = agent.respond("电池温度怎么样")
    assert ("℃" in resp_temp or re.search(r"\d{2}(\.\d)?\s*度", resp_temp)), \
        f"温度回答未包含温度值：{resp_temp[:120]}"
    resp_dr = agent.respond("有没有需求响应事件")
    assert isinstance(resp_dr, str) and len(resp_dr.strip()) > 0, "DR 问题没有回答"


def test_tool_numbers_match_report(tools):
    """工具输出里的收益数字必须与调度报表一致，不能是另一套写死的数"""
    t, report = tools
    text = t.get_report()
    nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.\d+", text)]
    assert any(abs(n - report.net_revenue_yuan) < 1.0 for n in nums), \
        f"工具输出里找不到与报表一致的净收益 {report.net_revenue_yuan}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
