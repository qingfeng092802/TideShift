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


# ---------- F2：来源 / 模式必须是机器可读字段（供 API 层透传） ----------

def test_agent_factory_sets_mode_flag(tools):
    """create_agent 返回的 Agent 必须带 mode 标识，调用方不必 isinstance 猜。"""
    t, _ = tools
    rule_agent = create_agent(t.ctx, api_key="")
    assert rule_agent.mode == "rule"

    pytest.importorskip("langchain_openai")
    from src.agents.chat_agent import LLMAgent
    # 构造不发起网络请求（仅建图），用假 Key 只为验证模式标识
    llm_agent = LLMAgent(t, api_key="sk-not-a-real-key", base_url=None, model="gpt-4o-mini")
    assert llm_agent.mode == "llm"


def test_explain_day_records_source_rule(tools):
    """有调度结果且未配置 Key → 走规则模板，来源必须落成 "rule"（不是 "none"）。"""
    t, _ = tools
    out = t.explain_day()
    assert isinstance(out, str) and len(out.strip()) > 0
    assert t.last_explain_source == "rule", t.last_explain_source
    # 正文前缀与来源字段必须一致，不能各说各话
    assert "规则模板解释" in out


def test_explain_day_source_none_without_schedule():
    """无调度结果时来源为 "none"，且不抛异常。"""
    t = SchedulingTools(AgentContext())
    assert t.explain_day() == "请先运行调度。"
    assert t.last_explain_source == "none"


def test_routing_does_not_misfire_on_dispatch_keywords(tools):
    """Bug 5：含「调度 / 协调」的提问不得被误路由到「调参重跑」分支。

    此前关键词列表含裸字 `"调"`，而「调度」「协调」都含「调」，
    导致「今天的调度策略是什么？」返回的是"我可以帮你调整参数重跑"提示，
    而不是调度解释。

    注意：本用例只用**无参数可提取**的调参语句（如"调整一下参数"），
    避免真的触发 run_with_params 重跑 MILP 而拖慢快测。
    """
    t, _ = tools
    agent = create_agent(t.ctx, api_key="")

    for q in ["今天的调度策略是什么？", "帮我协调一下充放电安排", "调度结果解释一下"]:
        resp = agent.respond(q)
        assert "调整参数重跑" not in resp, \
            f"「{q}」被误路由到调参分支（裸字 '调' 的误命中）：{resp[:120]}"

    # 真正的调参意图必须仍然命中（无参数可提取 → 返回引导语，不触发重跑）
    resp = agent.respond("帮我调整一下参数")
    assert "调整参数重跑" in resp, f"调参意图未被识别：{resp[:120]}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
