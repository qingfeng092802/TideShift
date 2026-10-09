"""评测运行器 —— 把 evals/cases.yaml 跑成指标。

三条设计约束，决定了这个文件为什么长成这样：

1. **一次求解，全程复用**。跑一次 96 点 MILP 要 38~43 秒，逐用例重跑会让评测
   无法进 CI。所有查询/解释类用例共享同一份调度结果，只有 act 类会触发重跑，
   而它们默认被 recorder 打桩。
2. **规则模式必须完全离线**。默认模式不联网、不需要 API Key，因此任何时候都能
   复现，报告也不会因为某个 Key 失效而变红。真实模型模式要显式开 --mode llm。
3. **只观测真实执行**。工具调用来自 recorder，不来自模型自述。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

EVALS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(EVALS_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from evals.grounding import run_probes                       # noqa: E402
from evals.metrics import (CaseResult, GroundingProbeResult, TOOL_NAMES,  # noqa: E402
                           score_case)
from evals.recorder import DEFAULT_STUB_TOOLS, install        # noqa: E402

CASES_PATH = os.path.join(EVALS_DIR, "cases.yaml")
REPORTS_DIR = os.path.join(EVALS_DIR, "reports")
BASELINE_PATH = os.path.join(REPORTS_DIR, "baseline.json")

# 用例允许的字段。拼错一个键名（expect_tool / must_constain）会静默失效——
# 那份评测就会"全绿"却什么都没测，所以这里显式拒绝未知字段。
ALLOWED_CASE_KEYS = frozenset({
    "id", "kind", "question", "modes", "desc",
    "expect_tools", "forbid_tools", "expect_args",
    "must_contain", "must_not_contain", "must_regex",
    "expect_from_report", "expect_literal_numbers", "number_tol", "literal_tol",
    "allow_side_effect",
})
ALLOWED_PROBE_KEYS = frozenset({"id", "desc", "planted_numbers", "answer_template"})
VALID_KINDS = frozenset({"query", "explain", "act", "guard"})


def validate_suite(suite: Dict[str, Any]) -> List[str]:
    """返回用例文件的毛病清单（空 = 干净）。"""
    problems: List[str] = []
    seen_ids = set()
    for case in suite.get("cases") or []:
        cid = str(case.get("id", "?"))
        if cid in seen_ids:
            problems.append(f"用例 id 重复：{cid}")
        seen_ids.add(cid)
        unknown = set(case) - ALLOWED_CASE_KEYS
        if unknown:
            problems.append(f"{cid}: 未知字段 {sorted(unknown)}（拼错会导致断言静默失效）")
        if not case.get("question"):
            problems.append(f"{cid}: 缺 question")
        if case.get("kind") not in VALID_KINDS:
            problems.append(f"{cid}: kind={case.get('kind')} 不在 {sorted(VALID_KINDS)}")
        for tool in (case.get("expect_tools") or []) + (case.get("forbid_tools") or []):
            if tool not in TOOL_NAMES:
                problems.append(f"{cid}: 工具名 {tool} 不存在")

    for probe in suite.get("grounding_probes") or []:
        pid = str(probe.get("id", "?"))
        unknown = set(probe) - ALLOWED_PROBE_KEYS
        if unknown:
            problems.append(f"{pid}: 未知字段 {sorted(unknown)}")
        if not probe.get("answer_template"):
            problems.append(f"{pid}: 缺 answer_template")
        for value in probe.get("planted_numbers") or []:
            float(value)   # 非数值在此抛错，交由调用方计入 problems 前先暴露
    return problems


@dataclass
class EvalEnv:
    """一次评测所需的全部共享状态（含一次真实求解）。"""
    tools: Any
    ctx: Any
    report: Any
    digest: Any
    recorder: Any
    date: str
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalRun:
    results: List[CaseResult] = field(default_factory=list)
    probes: List[GroundingProbeResult] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    errors: List[Tuple[str, str]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


def load_suite(path: Optional[str] = None) -> Dict[str, Any]:
    with open(path or CASES_PATH, "r", encoding="utf-8") as fh:
        suite = yaml.safe_load(fh) or {}
    suite.setdefault("defaults", {})
    suite.setdefault("cases", [])
    suite.setdefault("grounding_probes", [])
    return suite


def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=EVALS_DIR, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "?"
    except Exception:
        return "?"


def build_env(date: Optional[str] = None, use_ml_forecast: Optional[bool] = None,
              stub_act: bool = True, dr_fixture: bool = True) -> EvalEnv:
    """加载数据 → 跑一次日调度 → 装记录器 → 造事实摘要。

    DR 事件刻意给两个：一个可满足（走"接受"分支）、一个物理上不可能（走"拒绝"分支），
    否则 get_dr_info 的拒绝路径永远没被测到。
    """
    from src.agents.chat_agent import AgentContext, SchedulingTools
    from src.agents.coordinator_agent import CoordinatorAgent
    from src.agents.demand_response_agent import DRSignal
    from src.agents.storage_optimization_agent import StorageOptimizationAgent
    from src.data.data_generator import generate_ambient_temp, generate_price_profile
    from src.data.data_loader import load_load_data
    from src.utils.config import CONFIG

    suite = load_suite()
    day = date or str(suite["defaults"].get("date", "2024-07-30"))
    ml = bool(suite["defaults"].get("use_ml_forecast", False)) if use_ml_forecast is None \
        else bool(use_ml_forecast)

    dr_signals: List[Any] = []
    if dr_fixture:
        dr_signals = [
            DRSignal(start_time=f"{day} 15:00", end_time=f"{day} 17:00",
                     target_reduction_kw=400, subsidy_per_kwh=0.8, dr_type="peak_shaving"),
            DRSignal(start_time=f"{day} 19:00", end_time=f"{day} 20:00",
                     target_reduction_kw=5000, subsidy_per_kwh=0.8, dr_type="peak_shaving"),
        ]

    hist = load_load_data()
    coord = CoordinatorAgent(CONFIG)
    report = coord.run_daily_scheduling(
        date=day, historical_data=hist, dr_signals=dr_signals,
        use_ml_forecast=ml, include_thermal=True, include_degradation=True)

    import numpy as np
    from src.data.data_loader import day_price_temp
    # 与协调器同源取当日序列：`generate_ambient_temp` 含未播种噪声，直接调它会让
    # baseline 每天抽一条新气温曲线，评测里的对比项就跟着漂。
    pt = day_price_temp(hist, day)
    if pt is None:
        import pandas as pd
        tidx = pd.date_range(day, periods=CONFIG.battery.num_steps, freq="15min")
        pt = (np.asarray(generate_price_profile(tidx), dtype=float),
              np.asarray(generate_ambient_temp(tidx), dtype=float))
    price, amb = pt
    baseline = StorageOptimizationAgent(CONFIG).baseline_strategy(price, amb)

    viz = coord.get_visualization_data()
    ctx = AgentContext(report=report, coordinator=coord, baseline=baseline,
                       viz_data=viz, dr_signals=dr_signals, historical_data=hist,
                       selected_date=day,
                       schedule=viz.get("final_schedule") if viz else None)
    tools = SchedulingTools(ctx)
    rec = install(tools, stub_tools=DEFAULT_STUB_TOOLS if stub_act else ())
    digest = tools.current_digest()

    final = viz.get("final_schedule") if viz else None
    meta = {
        "date": day,
        "use_ml_forecast": ml,
        "solver_status": getattr(final, "solver_status", ""),
        "git_sha": _git_sha(),
        "python": sys.version.split()[0],
        "stub_act": stub_act,
        "n_dr_signals": len(dr_signals),
    }
    return EvalEnv(tools=tools, ctx=ctx, report=report, digest=digest,
                   recorder=rec, date=day, meta=meta)


def make_agent(env: EvalEnv, mode: str, model: str = "", api_key: str = "",
               base_url: str = "") -> Tuple[Any, Dict[str, Any]]:
    """按模式造对话体。两种模式都复用 env.tools，recorder 才能抓到真实调用。"""
    from src.agents.chat_agent import LLMAgent, RuleBasedAgent

    if mode == "rule":
        return RuleBasedAgent(env.tools), {"mode": "rule"}
    if not api_key:
        raise RuntimeError(
            "--mode llm 需要 API Key：设 DEEPSEEK_API_KEY / OPENAI_API_KEY / LLM_API_KEY "
            "或用 --api-key 传入。（规则模式无此要求：pytest 默认跑的就是规则模式）")
    agent = LLMAgent(env.tools, api_key=api_key, base_url=base_url or None,
                     model=model or "gpt-4o-mini")
    return agent, {"mode": "llm", "model": model or "gpt-4o-mini"}


def _ask(agent: Any, question: str) -> Tuple[str, Dict[str, int]]:
    """问一句，返回回答文本与 token 用量（能拿到就拿，拿不到不编）。"""
    usage: Dict[str, int] = {}
    executor = getattr(agent, "executor", None)
    if executor is not None:
        try:
            state = executor.invoke({"messages": [{"role": "user", "content": question}]})
            msgs = state.get("messages", []) if isinstance(state, dict) else []
            for msg in msgs:
                meta = getattr(msg, "usage_metadata", None)
                if meta:
                    usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + int(
                        meta.get("input_tokens") or 0)
                    usage["completion_tokens"] = usage.get("completion_tokens", 0) + int(
                        meta.get("output_tokens") or 0)
            if msgs:
                content = getattr(msgs[-1], "content", "")
                return content if isinstance(content, str) else str(content), usage
        except Exception as exc:
            return f"【评测：executor 调用失败】{type(exc).__name__}: {exc}", usage
    return agent.respond(question), usage


def applicable(case: Dict[str, Any], mode: str) -> Tuple[bool, str]:
    modes = case.get("modes")
    if modes and mode not in modes:
        return False, f"用例限定 {modes}，当前模式 {mode}"
    return True, ""


def run_suite(suite: Dict[str, Any], env: EvalEnv, mode: str = "rule",
              model: str = "", api_key: str = "", base_url: str = "",
              only_ids: Optional[Sequence[str]] = None) -> EvalRun:
    agent, agent_meta = make_agent(env, mode, model=model, api_key=api_key,
                                    base_url=base_url)
    run = EvalRun(meta={**env.meta, **agent_meta, "mode": mode})
    defaults = suite.get("defaults") or {}

    for case in suite.get("cases") or []:
        case = {**defaults, **case}
        cid = str(case.get("id", "?"))
        if only_ids and cid not in set(only_ids):
            continue
        ok, why = applicable(case, mode)
        if not ok:
            run.skipped.append((cid, why))
            continue

        env.recorder.reset()
        started = time.perf_counter()
        try:
            answer, usage = _ask(agent, str(case.get("question", "")))
        except Exception as exc:                     # agent 炸了要留痕，不能整轮评测跟着崩
            run.errors.append((cid, f"{type(exc).__name__}: {exc}"))
            continue
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        run.results.append(score_case(case, env.recorder.calls, answer,
                                      env.report, latency_ms=elapsed_ms, usage=usage))

    if env.digest is not None:
        run.probes = run_probes(env.digest, suite.get("grounding_probes") or [])
    else:
        run.errors.append(("grounding", "事实摘要为 None，探针未执行"))
    return run
