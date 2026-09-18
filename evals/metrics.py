"""评测指标 —— 纯函数，无 I/O、无模型调用，因此可被单元测试直接覆盖。

放在 evals/ 而不是 src/ 的理由：src/ 是交付运行的代码，评测是开发期的度量工具，
两者的依赖与生命周期不同；混在一起会让"测试跑了多少"与"agent 做对了多少"混淆。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

TOOL_NAMES: Tuple[str, ...] = (
    "get_report",
    "compare_baseline",
    "explain_schedule",
    "get_thermal_info",
    "get_dr_info",
    "run_with_params",
    "explain_day",
    "ask",
)


@dataclass
class ToolCall:
    """一次**真实执行过**的工具调用（由 recorder 记录，不是模型"说它想调"）。"""
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    returned: str = ""
    seq: int = 0


@dataclass
class CaseResult:
    """单个用例的判定明细。

    passed 是四个维度的 AND：工具选对 + 要点齐全 + 数字对得上 + 没有越权副作用。
    任一维度失手即记失败——评测集的作用是暴露缺陷，不是凑通过率。
    """
    case_id: str
    kind: str
    question: str
    passed: bool
    tool_ok: bool
    content_ok: bool
    number_ok: bool
    side_effect_ok: bool
    # 该用例是否声明了这两维的断言。未声明的用例不进对应指标的分母，
    # 否则"没人要求它核对数字"会被算成"数字全对"，指标虚高。
    checks_numbers: bool = True
    checks_side_effect: bool = True
    executed: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    latency_ms: float = 0.0
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    answer_len: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GroundingProbeResult:
    """防幻觉回查（check_grounding）自身被评测的结果。

    被测对象是**守卫本身**：喂给它一批已知含/不含编造数字的回答，看拦得住多少、
    误报多少。只举"拦住了 9999 元"一个例子没有说服力，必须给比率。
    """
    probe_id: str
    planted: List[str] = field(default_factory=list)     # 人为埋入的编造数字
    expected: List[str] = field(default_factory=list)    # 摘要里真实存在的数字
    flagged: List[str] = field(default_factory=list)     # check_grounding 报出的
    missed: List[str] = field(default_factory=list)
    false_positives: List[str] = field(default_factory=list)

    @property
    def caught_all(self) -> bool:
        return not self.missed

    def to_dict(self) -> dict:
        d = asdict(self)
        d["caught_all"] = self.caught_all
        return d


def number_in_text(value: float, text: str, tol: float = 1.0,
                   rel_tol: float = 0.0) -> bool:
    """回答里是否出现了给定数值。

    抽数比较而不是 `str(value) in text`：模型会把 1198.12 写成 1198、1,198.12
    或 约 1198 元，字符串包含会把正确答案判错。
    """
    nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")]
    limit = abs(value) * rel_tol + tol
    return any(abs(n - value) <= limit for n in nums)


def _arg_equal(actual: Any, want: Any) -> bool:
    # bool 必须单独比：Python 里 True == 1，混在数值分支会把参数断言判错
    if isinstance(want, bool) or isinstance(actual, bool):
        return bool(actual) == bool(want) if actual is not None else want is None
    if isinstance(want, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(actual) - float(want)) < 1e-6
    return actual == want


def check_tool_expectation(case: dict, calls: Sequence[ToolCall]) -> List[str]:
    """工具维度：期望工具（any-of）是否被调用、禁止工具是否出现、参数是否匹配。"""
    failures: List[str] = []
    executed = [c.tool for c in calls]
    expect = list(case.get("expect_tools") or [])
    forbid = list(case.get("forbid_tools") or [])

    for tool in forbid:
        if tool in executed:
            failures.append(f"调用了被禁止的工具 {tool}")

    if expect and not any(t in executed for t in expect):
        failures.append(f"未调用期望工具 {expect}，实际调用 {executed or '（无）'}")

    expect_args = case.get("expect_args")
    if expect_args and not failures:
        matched = False
        for call in calls:
            if call.tool not in expect:
                continue
            if all(_arg_equal(call.args.get(k), v) for k, v in expect_args.items()):
                matched = True
                break
        if not matched:
            failures.append(f"工具参数不符：期望至少一次 {expect_args}，"
                            f"实际 {[(c.tool, c.args) for c in calls]}")
    return failures


def check_content(case: dict, answer: str) -> List[str]:
    """内容维度：must_contain 全部命中，must_not_contain / must_regex 的约束不被破坏。"""
    failures: List[str] = []
    text = answer or ""
    for needle in case.get("must_contain") or []:
        if needle not in text:
            failures.append(f"缺少要点「{needle}」")
    for needle in case.get("must_not_contain") or []:
        if needle in text:
            failures.append(f"出现禁止表述「{needle}」")
    for pattern in case.get("must_regex") or []:
        if not re.search(pattern, text):
            failures.append(f"未匹配模式 /{pattern}/")
    return failures


def check_numbers(case: dict, answer: str, report: Any) -> List[str]:
    """数字维度：回答里必须出现与真实调度报表一致的数值。

    这是全评测集唯一"对答案"的一维——锚点取自运行时的 DailyReport，不写死在
    用例文件里，因此改模型、改提示词、改求解口径都会被这一维抓到。
    """
    failures: List[str] = []
    tol = float(case.get("number_tol", 1.0))
    for attr in case.get("expect_from_report") or []:
        value = getattr(report, attr, None)
        if value is None:
            failures.append(f"报表缺少字段 {attr}（用例与环境不符，非 agent 之过）")
            continue
        if not number_in_text(float(value), answer, tol=tol):
            failures.append(f"回答未含报表值 {attr}={float(value):.2f}")
    for value in case.get("expect_literal_numbers") or []:
        if not number_in_text(float(value), answer,
                              tol=float(case.get("literal_tol", 1.0))):
            failures.append(f"回答未含期望数值 {value}")
    return failures


def check_side_effect(case: dict, calls: Sequence[ToolCall]) -> List[str]:
    """副作用维度：不该重跑 MILP 的题不许重跑。

    单列一维而不并进工具判定，是因为"选错工具"与"不该有副作用却做了副作用"
    是两类缺陷，报告里要能分开看。
    """
    if case.get("allow_side_effect", True):
        return []
    return ["不该触发重跑却调用了 run_with_params"
            for c in calls if c.tool == "run_with_params"]


def declares_numbers(case: dict) -> bool:
    return bool(case.get("expect_from_report") or case.get("expect_literal_numbers"))


def declares_side_effect(case: dict) -> bool:
    return "allow_side_effect" in case


def score_case(case: dict, calls: Sequence[ToolCall], answer: str,
               report: Any, latency_ms: float = 0.0,
               usage: Optional[Dict[str, int]] = None) -> CaseResult:
    """把四个维度合成一个用例判定。"""
    f_tool = check_tool_expectation(case, calls)
    f_content = check_content(case, answer)
    f_number = check_numbers(case, answer, report)
    f_side = check_side_effect(case, calls)
    failures = f_tool + f_content + f_number + f_side
    usage = usage or {}

    return CaseResult(
        case_id=str(case.get("id", "?")),
        kind=str(case.get("kind", "query")),
        question=str(case.get("question", "")),
        passed=not failures,
        tool_ok=not f_tool,
        content_ok=not f_content,
        number_ok=not f_number,
        side_effect_ok=not f_side,
        checks_numbers=declares_numbers(case),
        checks_side_effect=declares_side_effect(case),
        executed=[c.tool for c in calls],
        failures=failures,
        latency_ms=latency_ms,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        answer_len=len(answer or ""),
    )


def percentile(values: Sequence[float], pct: float) -> float:
    """线性插值分位数，空序列返回 0.0。手写以免评测层为算分位引入依赖。"""
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return 0.0
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * (pct / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def _rate(pool: List[CaseResult], attr: str) -> Tuple[float, int]:
    """pool 为空时返回 NaN 而不是 1.0：没有样本不等于全对。"""
    if not pool:
        return (float("nan"), 0)
    return (round(sum(1 for r in pool if getattr(r, attr)) / len(pool), 4), len(pool))


def aggregate(results: Iterable[CaseResult],
              probes: Optional[Iterable[GroundingProbeResult]] = None) -> Dict[str, Any]:
    """汇总成一组比率指标，供报告展示与基线回归比较。"""
    rs = list(results)
    if not rs:
        return {"n_cases": 0}

    tool_rate, tool_n = _rate(rs, "tool_ok")
    content_rate, content_n = _rate(rs, "content_ok")
    num_pool = [r for r in rs if r.checks_numbers]
    number_rate, number_n = _rate(num_pool, "number_ok")
    side_pool = [r for r in rs if r.checks_side_effect]
    side_rate, side_n = _rate(side_pool, "side_effect_ok")

    lat = [r.latency_ms for r in rs if r.latency_ms]
    out: Dict[str, Any] = {
        "n_cases": len(rs),
        "n_passed": sum(1 for r in rs if r.passed),
        "task_success_rate": round(sum(1 for r in rs if r.passed) / len(rs), 4),
        "tool_call_accuracy": tool_rate,
        "tool_call_n": tool_n,
        "answer_completeness": content_rate,
        "answer_completeness_n": content_n,
        "number_fidelity": number_rate,
        "number_fidelity_n": number_n,
        "side_effect_guard_rate": side_rate,
        "side_effect_guard_n": side_n,
        "latency_p50_ms": round(percentile(lat, 50), 1),
        "latency_p95_ms": round(percentile(lat, 95), 1),
        "prompt_tokens": sum(r.prompt_tokens or 0 for r in rs),
        "completion_tokens": sum(r.completion_tokens or 0 for r in rs),
        "by_kind": {
            kind: {
                "n": len(pool),
                "task_success_rate": round(sum(1 for r in pool if r.passed) / len(pool), 4),
            }
            for kind, pool in _group_by_kind(rs)
        },
    }

    probes = list(probes or [])
    if probes:
        planted_total = sum(len(p.planted) for p in probes)
        missed_total = sum(len(p.missed) for p in probes)
        clean = [p for p in probes if not p.planted]
        out.update({
            "grounding_catch_rate": round((planted_total - missed_total) / planted_total, 4)
            if planted_total else float("nan"),
            "grounding_planted_numbers": planted_total,
            "grounding_missed_numbers": missed_total,
            "grounding_false_positive_rate": round(
                sum(1 for p in clean if p.false_positives) / len(clean), 4)
            if clean else float("nan"),
            "grounding_clean_probes_n": len(clean),
        })
    return out


def _group_by_kind(rs: List[CaseResult]):
    for kind in sorted({r.kind for r in rs}):
        yield kind, [r for r in rs if r.kind == kind]


# 参与回归比较的指标；延迟与 token 不参与（受机器与网络影响，只做观测）
REGRESSION_KEYS = (
    "task_success_rate",
    "tool_call_accuracy",
    "answer_completeness",
    "number_fidelity",
    "side_effect_guard_rate",
    "grounding_catch_rate",
    "grounding_false_positive_rate",
)


def compare_to_baseline(current: Dict[str, Any], baseline: Dict[str, Any],
                        tolerance: float = 0.02,
                        lower_is_better: Sequence[str] = ("grounding_false_positive_rate",)
                        ) -> List[str]:
    """与已提交基线比较，返回退化清单（空 = 无退化）。

    tolerance 默认 2 个百分点：规则模式是确定性的，理论上逐位相同；留余量是为了
    真实模型模式下同一套用例的采样抖动不至于天天报红。
    """
    regressions: List[str] = []
    for key in REGRESSION_KEYS:
        base, now = baseline.get(key), current.get(key)
        if not isinstance(base, (int, float)) or not isinstance(now, (int, float)):
            continue  # 任一侧缺项/NaN → 不参与比较，避免"没测"被当成"退化"或"合格"
        if key in lower_is_better:
            if now > base + tolerance:
                regressions.append(f"{key}: 基线 {base:.3f} → 当前 {now:.3f}（升高 {now - base:.3f}）")
        elif now < base - tolerance:
            regressions.append(f"{key}: 基线 {base:.3f} → 当前 {now:.3f}（退化 {base - now:.3f}）")
    return regressions
