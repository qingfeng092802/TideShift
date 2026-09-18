"""用评测层量 agent 本身 —— 需要一次真实 MILP 求解，因此标 slow，PR 快车道跳过。

nightly（`pytest -m ""`）会跑它。这里刻意**不**把 task_success_rate 写成断言阈值：
数值回归由 reports/baseline.json + compare_to_baseline 把关。写死阈值会让
"改了提示词导致退化"和"评测层自己坏了"报同一个错，反而看不出区别。
"""
import json
import os

import pytest

from evals.harness import BASELINE_PATH, build_env, load_suite, run_suite, validate_suite
from evals.metrics import aggregate, compare_to_baseline

pytestmark = [pytest.mark.eval, pytest.mark.slow]


@pytest.fixture(scope="module")
def rule_run():
    suite = load_suite()
    problems = validate_suite(suite)
    assert problems == [], f"用例文件有问题：{problems}"
    env = build_env()
    return run_suite(suite, env, mode="rule")


def test_environment_actually_solved(rule_run):
    """评测结论全部建立在"真的解出来一个可行方案"之上，先把它钉死。"""
    meta = rule_run.meta
    assert meta["solver_status"], "求解器没有返回状态，后面的数字都无意义"
    assert meta["stub_act"] is True, "默认必须打桩 act 重跑，否则本文件会跑到分钟级"


def test_every_case_ran_without_error(rule_run):
    assert not rule_run.errors, rule_run.errors
    assert len(rule_run.results) >= 25
    assert len(rule_run.skipped) < len(rule_run.results), "跳过比跑过的还多，用例集配置有问题"


def test_no_runtime_exceptions_in_answers(rule_run):
    for res in rule_run.results:
        assert "工具异常" not in " ".join(res.failures), res.case_id
        assert res.answer_len > 0, f"{res.case_id} 返回空回答"


def test_guard_dimension_is_perfect(rule_run):
    """副作用守卫必须 100%：这条不是质量指标，是安全边界。"""
    metrics = aggregate(rule_run.results, rule_run.probes)
    assert metrics["side_effect_guard_rate"] == 1.0, [
        r.case_id for r in rule_run.results if not r.side_effect_ok]


def test_number_fidelity_dimension_is_perfect(rule_run):
    metrics = aggregate(rule_run.results, rule_run.probes)
    assert metrics["number_fidelity"] == 1.0, [
        (r.case_id, r.failures) for r in rule_run.results if not r.number_ok]


def test_grounding_guard_has_no_false_positive(rule_run):
    """真数字被误报会把守卫变成噪音，用户很快就开始忽略所有告警。"""
    metrics = aggregate(rule_run.results, rule_run.probes)
    assert metrics["grounding_false_positive_rate"] == 0.0
    assert metrics["grounding_planted_numbers"] >= 3, "探针太少，捕获率没有意义"


def test_no_regression_against_baseline(rule_run):
    if not os.path.exists(BASELINE_PATH):
        pytest.skip("尚无 reports/baseline.json：先跑 python -m evals.run_eval --update-baseline")
    metrics = aggregate(rule_run.results, rule_run.probes)
    with open(BASELINE_PATH, "r", encoding="utf-8") as fh:
        baseline = json.load(fh).get("metrics", {})
    regressions = compare_to_baseline(metrics, baseline)
    assert not regressions, "相对基线退化：\n" + "\n".join(regressions)


def test_baseline_file_is_not_stale(rule_run):
    """基线里记录的用例数少于当前用例集，说明加了用例却没更新基线。"""
    if not os.path.exists(BASELINE_PATH):
        pytest.skip("尚无基线文件")
    with open(BASELINE_PATH, "r", encoding="utf-8") as fh:
        base_n = json.load(fh).get("metrics", {}).get("n_cases", 0)
    metrics = aggregate(rule_run.results, rule_run.probes)
    assert base_n >= metrics["n_cases"] * 0.8, (
        f"基线只覆盖 {base_n} 条用例，当前有 {metrics['n_cases']} 条；"
        f"请跑 python -m evals.run_eval --update-baseline")
