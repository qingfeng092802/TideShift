"""参数寻优 Agent 的测试。

除最后一条 slow 用例外，全部走注入的评价函数：真实 MILP 一发 0.4~48 秒，用假目标函数
才能把"寻优逻辑对不对"测到秒级；"能不能真解出来"由那条 slow 用例兜底。

假目标函数是**已知参数的已知函数**，所以"有没有找到更优"有唯一答案，不是自说自话。
可行性/安全性判据复用生产代码的 `_rejections`，不在测试里另写一份 —— 否则"护栏生效"
这句话测的就是测试自己编的规则。
"""
from typing import Any, Dict, List

import pytest

from src.agents.parameter_search_agent import (
    MATERIAL_GAIN_RATIO, Candidate, HeuristicProposer, LLMProposer,
    ParameterSearchAgent, SearchReport, _key, _resample, clamp_params)
from src.utils.config import CONFIG

DATE = "2024-07-30"


def _day_history():
    """直接用生产加载器读内置 30 天数据。自己拼一份 DataFrame 会让"能跑通"这句话
    只覆盖到测试自造的输入，真实 CSV 的列名与编码问题就测不到了。"""
    from src.data.data_loader import load_load_data
    return load_load_data()


def make_eval(profile, temp=lambda p: 40.0, safe_status="Optimal",
              calls: List[Dict[str, Any]] = None):
    """把净收益做成参数的已知函数。"""

    def _eval(params: Dict[str, Any], steps: int) -> Candidate:
        if calls is not None:
            calls.append(dict(params))
        cand = Candidate(
            params=dict(params), stage="search", solver_status=safe_status,
            net_revenue_yuan=float(profile(params)), max_temp_c=float(temp(params)),
            equivalent_cycles=1.5)
        cand.reject_reasons = ParameterSearchAgent._rejections(cand, CONFIG)
        return cand

    return _eval


# 默认 1000；900kW「最赚钱」但峰值温度越过 55℃ 停机线（不可选）；700kW 是可选里最好的。
# 键是 sorted(params.items()) 的 tuple-of-pairs，不是单层 tuple。
TOY_TABLE = {
    (): 1000.0,
    (("rated_power_kw", 700),): 1100.0,
    (("rated_power_kw", 900),): 5000.0,
    (("soc_max_pct", 80),): 1050.0,
}


def toy_profile(params: Dict[str, Any]) -> float:
    return TOY_TABLE.get(tuple(sorted(params.items())), 1000.0)


def toy_temp(params: Dict[str, Any]) -> float:
    """大功率 → 高温度：造出"收益最高但越 55℃ 停机线"的那类解。"""
    return 62.0 if int(params.get("rated_power_kw") or 0) >= 900 else 40.0


class ScriptProposer:
    """按脚本喂提案：用来构造启发式队列里不会出现的组合（如 900kW）。"""
    mode = "script"

    def __init__(self, seq: List[Dict[str, Any]]):
        self.seq = list(seq)
        self.i = 0

    def reset(self) -> None:
        self.i = 0

    def propose(self, history, cfg):
        if self.i >= len(self.seq):
            return None
        p = self.seq[self.i]
        self.i += 1
        return p


# ---------- 护栏 1：安全解优先于最优解 ----------

def test_unsafe_candidate_is_evaluated_but_never_selected():
    calls: List[Dict[str, Any]] = []
    agent = ParameterSearchAgent(
        evaluate=make_eval(toy_profile, toy_temp, calls=calls),
        verify_eval=make_eval(toy_profile, toy_temp),
        proposer=ScriptProposer([{"rated_power_kw": 900}, {"rated_power_kw": 700}]),
        max_steps=4, search_steps=24)
    report = agent.run(DATE, _day_history())

    unsafe = [c for c in report.candidates if not c.safe]
    assert unsafe and max(c.net_revenue_yuan for c in unsafe) == 5000.0, \
        "越温限的解应当更赚钱，否则这条测不到护栏"
    assert report.winner.params == {"rated_power_kw": 700}, "5000 元但越 55℃ 的解不可选"
    assert report.rejected_unsafe == 1


def test_allow_unsafe_winner_is_opt_in_only():
    """只有显式知情同意，越温限的解才可能被采纳。"""
    agent = ParameterSearchAgent(
        evaluate=make_eval(toy_profile, toy_temp),
        verify_eval=make_eval(toy_profile, toy_temp),
        proposer=ScriptProposer([{"rated_power_kw": 900}, {"rated_power_kw": 700}]),
        max_steps=4, search_steps=24, allow_unsafe_winner=True)
    report = agent.run(DATE, _day_history())
    assert report.winner.params == {"rated_power_kw": 900}


@pytest.mark.parametrize("knob", ["include_thermal", "enable_dr"])
def test_policy_knobs_are_never_searchable(knob):
    """热约束与 DR 开关是安全/结算策略项：收益再高也不许优化器提议。

    粗分辨率更放大了这条的必要性 —— 24 点实测峰值温度约 35℃，96 点同配置约 47℃，
    搜索阶段根本看不见越温限的后果。
    """
    checked, reason = clamp_params({knob: False}, CONFIG)
    assert checked is None and "安全策略项" in reason


def test_entering_derate_region_is_not_a_rejection_reason():
    """45~55℃ 允许线性降额运行（见 README 温度保护策略表），不构成拒绝理由。"""
    cand = Candidate(params={"rated_power_kw": 900}, solver_status="Optimal",
                     net_revenue_yuan=1000.0, max_temp_c=47.0)
    assert ParameterSearchAgent._rejections(cand, CONFIG) == []


# ---------- 护栏 2：同分辨率复验 ----------

def test_conclusion_uses_only_verified_same_resolution_numbers():
    """搜索阶段粗分辨率的数字不得进结论；进结论的只能是复验阶段那一对同路数。"""
    search_eval = make_eval(lambda p: 5000.0 if p else 1000.0)
    verify_eval = make_eval(lambda p: 900.0 if p else 1000.0)      # 复验反而更差
    agent = ParameterSearchAgent(evaluate=search_eval, verify_eval=verify_eval,
                                 proposer=HeuristicProposer(), max_steps=3,
                                 search_steps=24, verify_steps=96)
    report = agent.run(DATE, _day_history())

    assert report.winner and report.winner.net_revenue_yuan == 5000.0
    assert report.verified_winner.net_revenue_yuan == 900.0
    assert report.agent_won is False, "粗分辨率更好但复验更差，必须判未跑赢"
    assert "未跑赢" in report.verdict


def test_verify_refuses_non_default_resolution():
    """复验跑在非 96 点会静默解错问题：协调器按 CSV 取 96 点价格，改 num_steps
    等于只拿半天数据去解一整天。"""
    agent = ParameterSearchAgent()
    with pytest.raises(ValueError, match="分辨率"):
        agent.verify({}, 48, DATE, _day_history(), [])


# ---------- 护栏 3：预算与停止 ----------

def test_step_budget_respected():
    calls: List[Dict[str, Any]] = []
    agent = ParameterSearchAgent(evaluate=make_eval(toy_profile, calls=calls),
                                 verify_eval=make_eval(toy_profile),
                                 proposer=HeuristicProposer(), max_steps=2,
                                 search_steps=24)
    report = agent.run(DATE, _day_history())
    assert report.used_steps == 2
    assert len(calls) == 3                     # 默认 + 2 步提案
    assert report.stopped_by == "budget"


class _StopProposer:
    """第二次就喊停。模型能主动终止循环，是"控制流由它决定"的最低证据。"""
    mode = "stub"

    def __init__(self):
        self.n = 0

    def propose(self, history, cfg):
        self.n += 1
        return None if self.n > 1 else {"rated_power_kw": 700}


def test_proposer_can_stop_the_loop_itself():
    agent = ParameterSearchAgent(evaluate=make_eval(toy_profile),
                                 verify_eval=make_eval(toy_profile),
                                 proposer=_StopProposer(), max_steps=9, search_steps=24)
    report = agent.run(DATE, _day_history())
    assert report.stopped_by == "proposer_stop"
    assert report.used_steps == 1


def test_deadline_stops_before_next_proposal():
    calls: List[Dict[str, Any]] = []
    agent = ParameterSearchAgent(evaluate=make_eval(toy_profile, calls=calls),
                                 verify_eval=make_eval(toy_profile),
                                 proposer=HeuristicProposer(), max_steps=5,
                                 deadline_s=-1.0, search_steps=24)
    report = agent.run(DATE, _day_history())
    assert report.stopped_by == "deadline"
    assert len(calls) == 1, "只有默认那一发求解发生"


def test_out_of_bounds_proposal_costs_no_solve_and_loop_continues():
    class _Bad:
        mode = "stub"

        def __init__(self):
            self.n = 0

        def propose(self, history, cfg):
            self.n += 1
            if self.n == 1:
                return {"rated_power_kw": 99999}       # 越界
            if self.n == 2:
                return {"bogus_knob": 1}               # 未知旋钮
            return {"rated_power_kw": 700}              # 之后重复

    calls: List[Dict[str, Any]] = []
    agent = ParameterSearchAgent(evaluate=make_eval(toy_profile, calls=calls),
                                 verify_eval=make_eval(toy_profile),
                                 proposer=_Bad(), max_steps=5, search_steps=24)
    report = agent.run(DATE, _day_history())
    assert report.out_of_bounds == 2
    assert report.winner.params == {"rated_power_kw": 700}
    assert len(calls) == 2, "越界提案不该消耗一次求解"
    # 5 步预算 = 越界 2 + 真实评价 1 + 重复 2；重复同样不该白吃一次求解
    assert report.duplicate_proposals == 2


def test_infeasible_default_stops_immediately():
    agent = ParameterSearchAgent(
        evaluate=make_eval(toy_profile, safe_status="Infeasible"),
        verify_eval=make_eval(toy_profile),
        proposer=HeuristicProposer(), max_steps=5, search_steps=24)
    report = agent.run(DATE, _day_history())
    assert report.stopped_by == "default_infeasible"
    assert report.winner is None
    assert "默认配置不可行" in report.verdict


def test_no_improvement_skips_verification():
    """没找到更好的组合时不做复验：两发 96 点求解（各约 48 秒）要花在刀刃上。"""
    agent = ParameterSearchAgent(evaluate=make_eval(lambda p: 1000.0),
                                 verify_eval=make_eval(lambda p: 999999.0),
                                 proposer=HeuristicProposer(), max_steps=7,
                                 search_steps=24)
    report = agent.run(DATE, _day_history())
    assert report.winner is None and report.verified_winner is None
    assert "未找到优于默认约束" in report.verdict


# ---------- 护栏 4：小于求解器噪声的"提升"不算提升 ----------

def _agent_with_gain(gain_ratio):
    def profile(params):
        return 1000.0 if not params else 1000.0 * (1 + gain_ratio)

    return ParameterSearchAgent(
        evaluate=make_eval(profile), verify_eval=make_eval(profile),
        proposer=ScriptProposer([{"soc_max_pct": 80}]),
        max_steps=2, search_steps=24)


def test_gain_below_solver_noise_does_not_win():
    """mip_gap=1% 下同配置重复求解实测能差约 1%，半个门槛的"提升"是噪声不是战果。"""
    report = _agent_with_gain(MATERIAL_GAIN_RATIO / 2).run(DATE, _day_history())
    assert report.winner is None


def test_gain_above_solver_noise_wins():
    report = _agent_with_gain(MATERIAL_GAIN_RATIO * 3).run(DATE, _day_history())
    assert report.winner is not None and report.winner.params == {"soc_max_pct": 80}


# ---------- 参数校验 ----------

@pytest.mark.parametrize("params,why", [
    ({"soc_max_pct": 120}, "越界"),
    ({"rated_power_kw": 100}, "越界"),
    ({"soc_min_pct": 0}, "越界"),
    ({"soc_min_pct": 85}, "窗口过窄"),         # 85%~90% → 先说窗口，再说边界
    ({"soc_min_pct": 30, "soc_max_pct": 35}, "窗口过窄"),
    ({"whatever": 1}, "未知旋钮"),
    ({"soc_max_pct": "abc"}, "必须是整数"),
])
def test_clamp_rejects(params, why):
    checked, reason = clamp_params(params, CONFIG)
    assert checked is None and why in reason, reason


def test_clamp_accepts_string_int_in_range():
    checked, reason = clamp_params({"soc_max_pct": "82"}, CONFIG)
    assert reason == "" and checked == {"soc_max_pct": 82}


# ---------- 求解状态判据 ----------

@pytest.mark.parametrize("status,infeasible", [
    ("Optimal", False),
    ("Infeasible", True),          # 陷阱：它含子串 "feasible"
    ("Not Solved", True),
    ("Undefined", True),
    ("", True),
    ("Stopped (time limit, feasible incumbent)", False),
    ("Stopped (time limit)", True),
    ("Final_DR_Adjusted", False),          # DR 改写后的状态，不是求解器原始串
    ("Final_LangGraph_DR_Adjusted", False),
    ("Baseline", False),
])
def test_status_infeasible_covers_real_project_statuses(status, infeasible):
    """判"不可行"不能用 `"feasible" in status`：Infeasible 本身就含 feasible。"""
    assert ParameterSearchAgent._status_infeasible(status) is infeasible


def test_resample_blocks_are_averaged():
    import numpy as np
    out = _resample(np.arange(96, dtype=float), 24)
    assert len(out) == 24 and out[0] == pytest.approx(1.5)
    with pytest.raises(ValueError):
        _resample(np.arange(95, dtype=float), 24)


# ---------- 提案器 ----------

def test_heuristic_never_repeats_a_proposal():
    proposer = HeuristicProposer()
    proposer.reset()
    seen: List[str] = []
    history: List[Candidate] = []
    for _ in range(8):
        nxt = proposer.propose(history, CONFIG)
        if nxt is None:
            break
        assert _key(nxt) not in seen
        seen.append(_key(nxt))
        history.append(Candidate(params=nxt))
    assert len(seen) >= 4


def test_heuristic_queue_empties_out():
    """提案队列必须会枯竭：不然寻优只能靠预算上限兜着，白烧求解器。"""
    proposer = HeuristicProposer()
    proposer.reset()
    for _ in range(20):
        if proposer.propose([], CONFIG) is None:
            return
    pytest.fail("启发式提案器不会收敛")


def test_heuristic_replans_after_reset():
    proposer = HeuristicProposer()
    while proposer.propose([], CONFIG) is not None:
        pass
    assert proposer.propose([], CONFIG) is None, "已经枯竭后不该再凭空造提案"
    proposer.reset()
    assert proposer.propose([], CONFIG) is not None


@pytest.mark.parametrize("text,want_params,want_stop", [
    ('{"action":"try","params":{"soc_max_pct":80}}', {"soc_max_pct": 80}, ""),
    ('```json\n{"action":"stop","reason":"已无提升空间"}\n```', None, "已无提升空间"),
    ('前置废话 {"action":"try","params":{"rated_power_kw":700}} 后话',
     {"rated_power_kw": 700}, ""),
    ("它就没给 JSON", None, ""),
    ('{"action":"try","params":{}}', None, ""),
    # parse 只管"能不能解出一个动作"，边界与策略项由 clamp_params 在 propose 里把关
    ('{"action":"try","params":{"soc_max_pct":400}}', {"soc_max_pct": 400}, ""),
])
def test_llm_proposer_parses_actions(text, want_params, want_stop):
    params, stop = LLMProposer.parse(text)
    assert params == want_params
    assert stop == want_stop


def _llm_with(client):
    proposer = LLMProposer(api_key="sk-test", client=client)
    proposer.fallback = HeuristicProposer()
    proposer.fallback.reset()
    return proposer


def test_llm_proposer_uses_model_proposal():
    seen: List[List[Dict[str, Any]]] = []

    def client(messages, **kw):
        seen.append(messages)
        return '{"action":"try","params":{"soc_max_pct":78},"reason":"避开高 SOC 区"}'

    proposer = _llm_with(client)
    assert proposer.propose([Candidate(params={})], CONFIG) == {"soc_max_pct": 78}
    assert proposer.calls == 1 and proposer.fallbacks == 0
    assert "已试过的候选" in seen[0][-1]["content"]
    assert "include_thermal" in seen[0][0]["content"]     # 提示词里写明禁改项


def test_llm_proposer_falls_back_when_client_raises():
    def client(messages, **kw):
        raise RuntimeError("连接超时")

    proposer = _llm_with(client)
    got = proposer.propose([Candidate(params={})], CONFIG)
    assert got == {"rated_power_kw": 700}                  # 启发式的第一提案
    assert proposer.calls == 1 and proposer.fallbacks == 1


def test_llm_proposer_falls_back_on_invalid_json():
    proposer = _llm_with(lambda messages, **kw: "抱歉，我不确定")
    assert proposer.propose([Candidate(params={})], CONFIG) is not None
    assert proposer.fallbacks == 1


def test_llm_proposer_falls_back_on_out_of_range_params():
    proposer = _llm_with(
        lambda messages, **kw: '{"action":"try","params":{"soc_max_pct":400}}')
    assert proposer.propose([Candidate(params={})], CONFIG) is not None
    assert proposer.fallbacks == 1


def test_llm_proposer_falls_back_on_policy_knob_proposal():
    proposer = _llm_with(
        lambda messages, **kw: '{"action":"try","params":{"include_thermal":false}}')
    assert proposer.propose([Candidate(params={})], CONFIG) == {"rated_power_kw": 700}
    assert proposer.fallbacks == 1


def test_llm_proposer_without_key_never_calls_client():
    def client(messages, **kw):
        raise AssertionError("无 Key 不该发起调用")

    proposer = LLMProposer(api_key="", client=client)
    proposer.fallback = HeuristicProposer()
    assert proposer.propose([], CONFIG) is not None
    assert proposer.fallbacks == 1


# ---------- 报告 ----------

def test_report_markdown_keeps_search_and_verify_apart():
    report = SearchReport(date=DATE, search_steps=24, verify_steps=96, budget_steps=3,
                          used_steps=1, stopped_by="budget", agent_won=True,
                          improvement_yuan=50.0, improvement_pct=5.0,
                          verdict="agent 跑赢固定流水线 +50.00 元（+5.0%）")
    report.candidates.append(Candidate(params={}, stage="search",
                                       net_revenue_yuan=1000.0, max_temp_c=35.0,
                                       solver_status="Optimal"))
    report.verified_default = Candidate(params={}, stage="verify",
                                        net_revenue_yuan=1000.0, max_temp_c=47.0,
                                        solver_status="Final_DR_Adjusted")
    report.verified_winner = Candidate(params={"rated_power_kw": 700}, stage="verify",
                                       net_revenue_yuan=1050.0, max_temp_c=44.0,
                                       solver_status="Final_DR_Adjusted")
    md = report.to_markdown()
    assert "同分辨率复验" in md and "跨分辨率净收益不可比" in md
    assert "结论：agent 跑赢固定流水线 +50.00 元" in md
    assert "1050.00" in md and "Final_DR_Adjusted" in md
    d = report.to_dict()
    assert d["verified"]["resolution_points"] == 96
    assert d["verified"]["winner_yuan"] == 1050.0


def test_report_without_verdict_says_so():
    """没有结论必须显式写出来，不能留一行空白让人以为结论是好消息。"""
    assert "寻优未产生结论" in SearchReport(date=DATE).to_markdown()


# ---------- 真求解（nightly）----------

@pytest.mark.slow
def test_real_milp_search_runs_and_respects_safety():
    """真实 MILP：默认解必须可行，越温限的解必须被拒，粗分辨率温度必须明显偏低。"""
    agent = ParameterSearchAgent(proposer=HeuristicProposer(), max_steps=3,
                                 search_steps=24, verify_steps=96)
    report = agent.run(DATE, _day_history())
    assert report.candidates and report.candidates[0].solver_status
    assert report.candidates[0].net_revenue_yuan > 0
    for cand in report.candidates:
        if cand.max_temp_c > CONFIG.battery.temp_safe_max:
            assert not cand.safe, "越温限的解被当成可选，护栏失效"
    # 24 点看不见热峰值：这正是"安全策略项不进寻优"的量化理由
    assert report.candidates[0].max_temp_c < CONFIG.battery.temp_normal_max
