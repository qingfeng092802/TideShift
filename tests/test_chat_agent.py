"""
对话Agent测试（pytest，规则模式，无需 API Key）

原来是脚本式 print。现在断言工具函数真的返回了内容、且数字与调度结果一致。

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
    from src.data.data_loader import day_price_temp
    from src.data.data_generator import generate_price_profile, generate_ambient_temp
    # 取当日落盘序列而不是现场 generate：generate_ambient_temp 含未播种噪声，
    # 每次抽一条新气温曲线，baseline 的对比项就跟着漂。
    _pt = day_price_temp(df, DATE)
    if _pt is None:
        _tidx = _pd.date_range(DATE, periods=96, freq="15min")
        _pt = (_np.asarray(generate_price_profile(_tidx), dtype=float),
               _np.asarray(generate_ambient_temp(_tidx), dtype=float))
    _price, _amb = _pt
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
    """修复：不再只断言"非空"——同时校验回答内容的正确性
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


# ---------- F3：参数抽取与分支优先序（由 evals/ 评测暴露出的真实缺陷）----------

def _routed_tools(tools, question: str):
    """规则路由**实际**调了哪些工具 —— 用评测层的记录器看，不靠解析回答文本猜。

    注意必须把 fixture 里那个 SchedulingTools 实例直接交给 RuleBasedAgent：
    `create_agent(tools.ctx)` 会另建一个实例，记录器包在旧对象上就什么都抓不到。
    """
    from evals.recorder import install
    from src.agents.chat_agent import RuleBasedAgent
    agent = RuleBasedAgent(tools)
    rec = install(tools)
    try:
        answer = agent.respond(question)
    finally:
        rec.uninstall()
    return rec, answer


@pytest.mark.parametrize("utterance,want", [
    # README 的示例句：贪婪 {0,5} 会把 "80%" 截成 "0"，按 soc_max=0 去跑真实 MILP
    ("把SOC上限调到80%重跑", {"soc_max": 80}),
    ("把 SOC 上限调到 80% 重跑", {"soc_max": 80}),
    ("SOC下限改成30%再算一次", {"soc_min": 30}),
    ("SOC上限调到85%、额定功率800kW重跑", {"soc_max": 85, "rated_power": 800}),
    ("关掉热约束重新跑一遍", {"include_thermal": False}),
    ("不参与需求响应，再算一次", {"enable_dr": False}),
    ("soc上限调到999重跑", {}),          # 越界值不传，交给上层用默认约束
])
def test_param_extraction_from_utterance(utterance, want):
    from src.agents.chat_agent import RuleBasedAgent
    agent = RuleBasedAgent(None)         # _extract_params 不触碰 tools
    assert agent._extract_params(utterance) == want, utterance


def test_run_verb_wins_over_bare_thermal_keyword(tools):
    """「关掉热约束重新跑一遍」曾被裸字 "热" 劫持到温度查询。"""
    t, _ = tools
    rec, _ = _routed_tools(t, "关掉热约束重新跑一遍")
    assert [c.tool for c in rec.calls] == ["run_with_params"]
    assert rec.calls[0].args["include_thermal"] is False


def test_run_verb_wins_over_dr_query(tools):
    t, _ = tools
    rec, _ = _routed_tools(t, "不参与需求响应，再算一次")
    assert [c.tool for c in rec.calls] == ["run_with_params"]
    assert rec.calls[0].args["enable_dr"] is False


def test_bare_run_verb_without_params_still_answers_query(tools):
    """只说"重新算一下和基准的对比"却没给参数：不该回调参引导语，该给对比结果。"""
    t, _ = tools
    rec, answer = _routed_tools(t, "帮我重新算一下和基准的对比")
    assert [c.tool for c in rec.calls] == ["compare_baseline"], answer


@pytest.mark.parametrize("question,attr", [
    ("今天等效循环多少次", "equivalent_cycles"),
    ("今天充了多少度电", "charge_energy_kwh"),
    ("今天的电池衰减成本是多少", "degradation_cost_yuan"),
])
def test_report_answers_carry_matching_numbers(tools, question, attr):
    """报表里已有的指标，问得到、且数字与报表一致（不是回一句引导语）。"""
    from evals.metrics import number_in_text
    t, report = tools
    rec, answer = _routed_tools(t, question)
    assert [c.tool for c in rec.calls] == ["get_report"], answer
    assert number_in_text(float(getattr(report, attr)), answer, tol=0.5), answer


# ========== 流式输出：工具结果不许冒充模型的话 ==========

def test_tool_chunks_are_recognised_and_dropped():
    """`stream_mode="messages"` 连 ToolMessage 一起产出，必须识别出来。

    实测过的症状：用户开了「流式输出」却看到回答"一次性全出来"——因为第一个
    delta 是整段工具报表原文（0.57 s 一次性到达），模型自己的几十个 token
    挤在其后约 170 ms 内，视觉上就是"瞬间出现"。
    """
    from src.agents.chat_agent import _is_tool_chunk

    class Toolish:
        type = "tool"

    class AIMessageChunkish:
        type = "ai"

    assert _is_tool_chunk(Toolish())
    assert not _is_tool_chunk(AIMessageChunkish())
    # 没有 type 属性时按类名兜底，别把未知类型整块当增量放行
    assert _is_tool_chunk(type("ToolMessage", (), {})())


def test_astream_yields_only_model_deltas():
    """astream 产出 ("content"|"reasoning", 文本)，工具消息的文本不许出现。"""
    import asyncio
    from src.agents.chat_agent import LLMAgent

    class Chunk:
        def __init__(self, text, kind, reasoning=""):
            self.content, self.type = text, kind
            self.additional_kwargs = {"reasoning_content": reasoning} if reasoning else {}

    class FakeExecutor:
        async def astream(self, _inp, stream_mode=None):
            assert stream_mode == "messages", "端点依赖 messages 模式，改这里要一起改"
            yield Chunk("📊 能效报表：净收益 1567.82 元", "tool"), {}
            yield Chunk("", "ai", "先取报表再换算"), {}
            yield Chunk("今日", "ai"), {}
            yield Chunk("", "ai"), {}            # 空增量应被跳过
            yield Chunk("净收益 1567.82 元", "ai"), {}

    agent = LLMAgent.__new__(LLMAgent)          # 不触发 langchain 初始化
    agent.executor = FakeExecutor()
    out = asyncio.run(_collect(agent))
    assert out == [("reasoning", "先取报表再换算"),
                   ("content", "今日"),
                   ("content", "净收益 1567.82 元")], out
    # 思维链与正文必须是两种 kind：混进正文会进 history 并被当正文渲染
    assert all(k in ("content", "reasoning") for k, _ in out)


async def _collect(agent):
    return [(k, d) async for k, d in agent.astream("今天净收益多少")]


def test_no_key_fallback_says_streaming_will_not_work(caplog):
    """没配 Key 时回退规则模式必须留下话，并且点名"流式开关此时不生效"。

    守的是可诊断性：规则模式没有 astream，`use_stream` 恒为 False，
    日志里什么都不写的话，排查的人会先去查 langchain 装没装（这次就白查了一轮）。
    """
    import logging
    from src.agents.chat_agent import RuleBasedAgent, AgentContext, create_agent

    with caplog.at_level(logging.INFO, logger="energy_dispatch.chat_agent"):
        agent = create_agent(AgentContext(), api_key="")
    assert isinstance(agent, RuleBasedAgent)
    assert not hasattr(agent, "astream")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "未配置 API Key" in joined and "流式" in joined
    # 日志不算交付：界面拿得到原因才算透传
    assert "未配置 API Key" in agent.fallback_reason


def test_fallback_reason_survives_init_failure(monkeypatch):
    """LLM 初始化抛异常时，原因要挂在 agent 上，而不是只进日志。"""
    import src.agents.chat_agent as mod
    from src.agents.chat_agent import AgentContext, RuleBasedAgent

    def boom(*_a, **_k):
        raise RuntimeError("base url 拒绝连接")

    monkeypatch.setattr(mod, "LLMAgent", boom)
    agent = create_agent(AgentContext(), api_key="sk-x", model="deepseek-chat")
    assert isinstance(agent, RuleBasedAgent)
    assert "base url 拒绝连接" in agent.fallback_reason


@pytest.mark.parametrize("utterance,want_tool", [
    # 「协调一下」含子串"调一下"，曾被当成调参动作词，真的跑了一次 MILP
    ("帮我协调一下SOC上限和负荷的关系", None),
    ("今天的调度策略是按什么安排的", None),
    ("address the throughput issue", None),        # 裸子串 "dr" 不再命中
    ("把SOC上限调到80%重跑", "run_with_params"),
    ("调整为放电功率1200kW再跑", "run_with_params"),
])
def test_adjust_verb_requires_real_verb(tools, utterance, want_tool):
    t, _ = tools
    rec, answer = _routed_tools(t, utterance)
    got = [c.tool for c in rec.calls]
    if want_tool is None:
        assert got != ["run_with_params"], f"{utterance} 不该触发重跑，实得 {answer}"
    else:
        assert got == [want_tool], answer


def test_llm_params_reach_the_chat_model():
    """设置页的 temperature/max_tokens/timeout 必须真的进模型构造参数。

    此前硬编码 temperature=0、从不带 max_tokens：界面上调采样参数没反应。
    测纯函数 `_llm_kwargs`，不构造 langchain 对象（那要联网装依赖）。
    """
    from src.agents.chat_agent import _llm_kwargs

    kw = _llm_kwargs("sk-x", "https://api.deepseek.com", "deepseek-chat",
                     {"temperature": 0.7, "max_tokens": 4096, "timeout": 30})
    assert (kw["temperature"], kw["max_tokens"], kw["timeout"]) == (0.7, 4096, 30)
    # Base URL 归一化：DeepSeek 文档里带 /v1 与不带两种写法都要能打通
    assert kw["base_url"] == "https://api.deepseek.com/v1"
    # 没配 llm_params 时退回旧行为（temperature 0、不限长），不是凭空造限制
    empty = _llm_kwargs("sk-x", "", "m", None)
    assert empty["temperature"] == 0 and "max_tokens" not in empty
    assert "base_url" not in empty


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
