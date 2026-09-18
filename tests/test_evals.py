"""evals 层自身的测试 —— 快车道，不需要求解器、不需要网络、不需要 API Key。

分工：这里测"尺子准不准"，`tests/test_evals_run.py` 用这把尺子量 agent。
指标阈值不写在这里：数值回归由 `evals/reports/baseline.json` + `--check-baseline` 把关，
把阈值硬编码进断言会让"改了提示词"和"评测坏了"两种情况报同一个错。
"""
import math

import pytest

from evals.grounding import render_probe, run_probe
from evals.harness import load_suite, validate_suite
from evals.metrics import (TOOL_NAMES, ToolCall, aggregate, check_tool_expectation,
                           compare_to_baseline, number_in_text, percentile, score_case)
from evals.recorder import install


class _Report:
    net_revenue_yuan = 1198.12
    arbitrage_revenue_yuan = 1702.79
    degradation_cost_yuan = 504.67
    dr_subsidy_yuan = 419.35
    max_battery_temp_c = 46.99
    equivalent_cycles = 1.8925
    charge_energy_kwh = 3005.5


class _FakeTools:
    """只提供 evals.recorder 需要包装的方法，不引任何真实依赖。"""

    def __init__(self):
        self.real_run_calls = 0

    def get_report(self):
        return "📊 日净收益 1198.12 元"

    def explain_schedule(self, hour):
        return f"⏰ {hour}:00 放电"

    def run_with_params(self, soc_min=None, soc_max=None, rated_power=None,
                        include_thermal=None, enable_dr=None):
        self.real_run_calls += 1
        return "真实重跑了（测试里不该发生）"


# ---------- 数字保真 ----------

@pytest.mark.parametrize("text,value,expect", [
    ("净收益 1,198.12 元", 1198.12, True),
    ("净收益约 1198 元", 1198.12, True),
    ("净收益 4521.00 元", 1198.12, False),
    ("最高温 47 ℃", 46.99, True),
    ("没有数字", 46.99, False),
])
def test_number_in_text_tolerates_formatting(text, value, expect):
    """千分位与取整是模型的正常表达，不该被判成"数字错了"。"""
    assert number_in_text(value, text, tol=0.55) is expect


# ---------- 工具维度 ----------

def test_missing_expected_tool_is_failure():
    case = {"id": "x", "expect_tools": ["get_report"]}
    assert check_tool_expectation(case, [])


def test_forbidden_tool_is_failure():
    case = {"id": "x", "forbid_tools": ["run_with_params"]}
    calls = [ToolCall(tool="run_with_params", args={"soc_max": 80})]
    failures = check_tool_expectation(case, calls)
    assert any("禁止" in f for f in failures)


def test_expect_args_requires_matching_call():
    case = {"id": "x", "expect_tools": ["run_with_params"], "expect_args": {"soc_max": 80}}
    assert not check_tool_expectation(case, [ToolCall("run_with_params", {"soc_max": 80})])
    assert check_tool_expectation(case, [ToolCall("run_with_params", {"soc_max": 90})])


def test_bool_arg_does_not_match_int():
    """False 不能因为 True == 1 这类隐式相等被匹配上。"""
    case = {"id": "x", "expect_tools": ["run_with_params"],
            "expect_args": {"include_thermal": False}}
    wrong = [ToolCall("run_with_params", {"include_thermal": 1})]
    right = [ToolCall("run_with_params", {"include_thermal": False})]
    assert check_tool_expectation(case, wrong)
    assert not check_tool_expectation(case, right)


# ---------- 分母口径 ----------

def test_number_metric_excludes_cases_without_anchors():
    """没有数字锚点的用例不该被算进"数字一致率"，否则该指标恒为 1。"""
    case = {"id": "no-anchor", "kind": "query", "question": "温度怎么样"}
    result = score_case(case, [ToolCall("get_thermal_info")], "最高 46.99℃", _Report())
    assert result.checks_numbers is False
    metrics = aggregate([result])
    assert math.isnan(metrics["number_fidelity"])
    assert metrics["number_fidelity_n"] == 0


def test_side_effect_denominator_only_counts_declared_cases():
    case = {"id": "x", "kind": "query", "question": "收益", "allow_side_effect": False}
    result = score_case(case, [ToolCall("run_with_params")], "净收益 1198.12 元", _Report())
    assert result.side_effect_ok is False
    metrics = aggregate([result])
    assert metrics["side_effect_guard_rate"] == 0.0
    assert metrics["side_effect_guard_n"] == 1


def test_task_success_requires_every_dimension():
    """四维 AND：数字错一个也不能算通过。"""
    case = {"id": "x", "kind": "query", "question": "净收益是多少",
            "expect_tools": ["get_report"], "expect_from_report": ["net_revenue_yuan"]}
    good = score_case(case, [ToolCall("get_report")], "净收益 1198.12 元", _Report())
    bad_number = score_case(case, [ToolCall("get_report")], "净收益 4521.00 元", _Report())
    assert good.passed and not bad_number.passed


def test_empty_results_yield_no_metrics():
    assert aggregate([]) == {"n_cases": 0}


def test_percentile_interpolates():
    assert percentile([], 50) == 0.0
    assert percentile([10], 95) == 10.0
    assert percentile([0, 10], 50) == 5.0


# ---------- 基线回归 ----------

def test_regression_detected_on_drop():
    base = {"tool_call_accuracy": 0.95}
    cur = {"tool_call_accuracy": 0.80}
    assert compare_to_baseline(cur, base)


def test_false_positive_rate_regression_is_upward():
    """误报率是"越低越好"的指标，退化方向必须反过来。"""
    base = {"grounding_false_positive_rate": 0.0}
    assert compare_to_baseline({"grounding_false_positive_rate": 0.5}, base)
    assert not compare_to_baseline({"grounding_false_positive_rate": 0.0}, base)


def test_missing_or_nan_baseline_key_is_skipped():
    """一侧没测过的指标不能凭空判成退化，也不能判成合格。"""
    assert not compare_to_baseline({"tool_call_accuracy": 0.1}, {"tool_call_accuracy": float("nan")})
    assert not compare_to_baseline({}, {"tool_call_accuracy": 0.9})


# ---------- 记录器 ----------

def test_recorder_captures_positional_args():
    tools = _FakeTools()
    rec = install(tools)
    tools.explain_schedule(15)
    assert rec.calls[0].args == {"hour": 15}


def test_stub_blocks_real_milp_rerun():
    """act 用例默认打桩：参数照记，求解器一次都不该被叫醒。"""
    tools = _FakeTools()
    rec = install(tools)
    out = tools.run_with_params(soc_max=80)
    assert "评测桩" in out
    assert tools.real_run_calls == 0
    assert rec.calls[0].args == {"soc_max": 80}


def test_uninstall_restores_original_methods():
    tools = _FakeTools()
    rec = install(tools)
    rec.uninstall()
    assert "真实重跑" in tools.run_with_params(soc_max=80)
    assert tools.real_run_calls == 1


def test_tool_exception_is_recorded_not_raised():
    class _Boom(_FakeTools):
        def get_report(self):
            raise RuntimeError("boom")

    tools = _Boom()
    rec = install(tools)
    assert "工具异常" in tools.get_report()
    assert rec.calls[0].args.get("_raised") == "RuntimeError"


# ---------- 防幻觉守卫探针 ----------

@pytest.fixture()
def digest():
    from src.agents.llm_explainer import DecisionDigest
    return DecisionDigest(
        date="2024-07-30", arbitrage_revenue_yuan=1702.79, dr_subsidy_yuan=0.0,
        degradation_cost_yuan=504.67, net_revenue_yuan=1198.12,
        charge_energy_kwh=3005.5, discharge_energy_kwh=2712.4,
        max_temp_c=46.99, avg_temp_c=38.2, temp_headroom_c=8.01,
        equivalent_cycles=1.8925, terminal_soc=0.5,
    )


def _probe(case_id):
    suite = load_suite()
    return next(p for p in suite["grounding_probes"] if p["id"] == case_id)


def test_probe_renders_from_digest(digest):
    text = render_probe(_probe("gp-clean-report"), digest)
    assert "1198.12" in text and "{" not in text


def test_guard_catches_planted_money(digest):
    result = run_probe(_probe("gp-fabricated-money"), digest)
    assert result.caught_all, f"漏网 {result.missed}"
    assert not result.false_positives, f"真数字被误报 {result.false_positives}"


def test_guard_catches_fabricated_temperature(digest):
    result = run_probe(_probe("gp-fabricated-temp"), digest)
    assert "71.5" in result.flagged[0] or result.caught_all


def test_guard_does_not_flag_rounded_true_numbers(digest):
    """取整是真数字的正常表达；拦它 = 误报，会把守卫变成噪音。"""
    result = run_probe(_probe("gp-rounding-ok"), digest)
    assert not result.false_positives, result.flagged


def test_guard_ignores_unitless_counts(digest):
    result = run_probe(_probe("gp-count-words"), digest)
    assert not result.flagged


def test_known_blind_spot_unit_outside_whitelist(digest):
    """"倍"不在单位白名单里，守卫看不见这个数 —— 已知盲区，故意留在评测集里。

    断言"漏网"而不是"拦住"：这条记录的是当前实现的边界，改进了守卫它就红，
    提醒你回来把断言翻过来，而不是让盲区悄悄变成通过。
    """
    result = run_probe(_probe("gp-unit-boundary"), digest)
    assert result.missed == ["0.9"]


# ---------- 用例文件本身的完整性 ----------

def test_cases_yaml_is_clean():
    suite = load_suite()
    assert validate_suite(suite) == []


def test_cases_cover_every_tool():
    """八个工具里若有一个从未被任何用例覆盖，那个工具的准确率就是虚构的。"""
    suite = load_suite()
    covered = set()
    for case in suite["cases"]:
        covered |= set(case.get("expect_tools") or [])
    uncovered = set(TOOL_NAMES) - covered
    assert not uncovered, f"未被任何用例覆盖的工具：{sorted(uncovered)}"


def test_cases_have_minimum_size():
    suite = load_suite()
    assert len(suite["cases"]) >= 25, "用例太少，指标没有统计意义"
    kinds = {c["kind"] for c in suite["cases"]}
    assert {"query", "explain", "act", "guard"} <= kinds
