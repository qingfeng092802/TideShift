"""评测入口 CLI。

    # 离线规则模式（默认）：不需要网络与 API Key，任何时候都能复现
    python -m evals.run_eval

    # 真实模型模式：显式开，凭据走环境变量
    DEEPSEEK_API_KEY=sk-... python -m evals.run_eval --mode llm --model deepseek-chat

    # 把当前结果写成回归基线（此后任何指标退化都会让 --check-baseline 退出码非 0）
    python -m evals.run_eval --update-baseline

退出码：0 正常；1 指标相对基线退化（仅当 --check-baseline）；2 环境/参数错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from evals.harness import (BASELINE_PATH, REPORTS_DIR, build_env, load_suite,
                           run_suite)
from evals.metrics import aggregate, compare_to_baseline

_MODES = ("rule", "llm")


def _env_key():
    """从环境变量解析真实模型凭据（复用解释层的同一优先级链，不另写一套）。"""
    from src.agents.llm_explainer import resolve_llm_credentials
    return resolve_llm_credentials()


def _payload(metrics: Dict[str, Any], run) -> Dict[str, Any]:
    return {
        "meta": {**run.meta, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
        "metrics": metrics,
        "results": [r.to_dict() for r in run.results],
        "probes": [p.to_dict() for p in run.probes],
        "skipped": [{"id": i, "reason": r} for i, r in run.skipped],
        "errors": [{"id": i, "error": e} for i, e in run.errors],
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return "—" if v != v else f"{v:.3f}"
    return str(v)


def render_markdown(payload: Dict[str, Any], regressions: List[str]) -> str:
    m = payload["metrics"]
    meta = payload["meta"]
    lines: List[str] = [
        "# TideShift 对话/解释层评测报告",
        "",
        f"- 模式：**{meta.get('mode')}**"
        + (f"（模型 `{meta.get('model')}`）" if meta.get("model") else "（规则路由，离线确定性）"),
        f"- 调度日 {meta.get('date')}｜求解器状态 `{meta.get('solver_status')}`"
        f"｜XGBoost {'开' if meta.get('use_ml_forecast') else '关'}"
        f"｜MILP 重跑 {'打桩' if meta.get('stub_act') else '真实执行'}",
        f"- 代码 `{meta.get('git_sha')}`｜Python {meta.get('python')}｜生成于 "
        f"{meta.get('generated_at')}",
        f"- 用例 {m.get('n_cases')} 条，跳过 {len(payload['skipped'])} 条，"
        f"执行异常 {len(payload['errors'])} 条",
        "",
        "## 指标",
        "",
        "| 指标 | 值 | 样本 | 含义 |",
        "|---|---|---|---|",
    ]
    rows = [
        ("task_success_rate", "n_cases", "四维全对的用例占比（主指标）"),
        ("tool_call_accuracy", "tool_call_n", "选对工具（含参数匹配）的比例"),
        ("answer_completeness", "answer_completeness_n", "回答要点齐全的比例"),
        ("number_fidelity", "number_fidelity_n", "数字与真实报表一致的比例"),
        ("side_effect_guard_rate", "side_effect_guard_n", "不该重跑 MILP 时确实没重跑"),
        ("grounding_catch_rate", "grounding_planted_numbers", "编造数字被守卫拦下的比例"),
        ("grounding_false_positive_rate", "grounding_clean_probes_n", "真数字被误报的比例（越低越好）"),
    ]
    for key, sample_key, desc in rows:
        lines.append(f"| `{key}` | {_fmt(m.get(key))} | {m.get(sample_key, '—')} | {desc} |")
    lines += [
        "",
        f"- 延迟 p50 / p95：{m.get('latency_p50_ms')} ms / {m.get('latency_p95_ms')} ms",
        f"- token：prompt {m.get('prompt_tokens')} / completion {m.get('completion_tokens')}"
        "（规则模式恒为 0，不参与结论）",
        "",
        "## 按用例类型",
        "",
        "| 类型 | 用例数 | 任务成功率 |",
        "|---|---|---|",
    ]
    for kind, val in (m.get("by_kind") or {}).items():
        lines.append(f"| {kind} | {val['n']} | {val['task_success_rate']:.3f} |")

    failed = [r for r in payload["results"] if not r["passed"]]
    lines += ["", f"## 失败用例（{len(failed)} 条）", ""]
    if not failed:
        lines.append("无。")
    else:
        lines += ["| 用例 | 类型 | 问题 | 实际调用 | 失败原因 |", "|---|---|---|---|---|"]
        for r in failed:
            lines.append(
                f"| `{r['case_id']}` | {r['kind']} | {r['question']} | "
                f"{'→'.join(r['executed']) or '（无）'} | "
                + "；".join(f"`{f}`" for f in r["failures"]) + " |")

    probes = payload["probes"]
    lines += ["", "## 防幻觉守卫探针", "",
              "| 探针 | 埋入的编造数字 | 被拦 | 漏网 | 误报 |", "|---|---|---|---|---|"]
    for p in probes:
        lines.append(f"| `{p['probe_id']}` | {p['planted'] or '—'} | {p['flagged'] or '—'} | "
                     f"{p['missed'] or '—'} | {p['false_positives'] or '—'} |")

    if payload["skipped"]:
        lines += ["", "## 跳过（模式不适用，不计入分母）", ""]
        lines += [f"- `{i}`：{r}" for i, r in ((s["id"], s["reason"]) for s in payload["skipped"])]
    if payload["errors"]:
        lines += ["", "## 执行异常", ""]
        lines += [f"- `{e['id']}`：{e['error']}" for e in payload["errors"]]

    lines += ["", "## 与基线比较", ""]
    if regressions:
        lines += [f"- ⚠️ {r}" for r in regressions]
    else:
        lines.append("- 无退化。")
    lines += ["", "> 口径：规则模式不联网、不依赖 API Key，是 CI 快车道跑的；",
              "> 真实模型模式需 `--mode llm` 显式开启，其指标随模型与采样浮动，",
              "> 因此基线回归阈值取 2 个百分点而非 0。", ""]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="TideShift agent 评测")
    ap.add_argument("--mode", choices=_MODES, default="rule")
    ap.add_argument("--date", default=None, help="调度日，默认取 cases.yaml 的 defaults.date")
    ap.add_argument("--only", nargs="*", default=None, help="只跑指定用例 id")
    ap.add_argument("--model", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--no-stub-act", action="store_true",
                    help="不打桩 run_with_params：act 用例将真实重跑 MILP（慢，分钟级）")
    ap.add_argument("--update-baseline", action="store_true", help="把本次指标写为回归基线")
    ap.add_argument("--check-baseline", action="store_true",
                    help="与已提交基线比较，退化则退出码 1")
    ap.add_argument("--out-prefix", default=None, help="报告输出前缀，默认 reports/<mode>")
    ap.add_argument("--cases", default=None, help="自定义用例文件路径")
    args = ap.parse_args(argv)

    suite = load_suite(args.cases)
    api_key, env_url, env_model = ("", "", "")
    if args.mode == "llm":
        api_key, env_url, env_model = _env_key()
        api_key = args.api_key or api_key
        if not api_key:
            print("错误：--mode llm 需要 API Key（--api-key 或 DEEPSEEK_API_KEY/"
                  "OPENAI_API_KEY/LLM_API_KEY）。", file=sys.stderr)
            return 2

    t0 = time.perf_counter()
    try:
        env = build_env(date=args.date, stub_act=not args.no_stub_act)
    except Exception as exc:
        print(f"错误：评测环境构建失败 —— {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    run = run_suite(suite, env, mode=args.mode,
                    model=args.model or env_model, api_key=api_key,
                    base_url=args.base_url or env_url, only_ids=args.only)
    metrics = aggregate(run.results, run.probes)
    payload = _payload(metrics, run)
    payload["meta"]["wall_seconds"] = round(time.perf_counter() - t0, 1)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    prefix = args.out_prefix or os.path.join(REPORTS_DIR, args.mode)
    with open(f"{prefix}-latest.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    regressions: List[str] = []
    if os.path.exists(BASELINE_PATH):
        with open(BASELINE_PATH, "r", encoding="utf-8") as fh:
            baseline = json.load(fh).get("metrics", {})
        regressions = compare_to_baseline(metrics, baseline)

    with open(f"{prefix}-latest.md", "w", encoding="utf-8") as fh:
        fh.write(render_markdown(payload, regressions))

    if args.update_baseline:
        with open(BASELINE_PATH, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"已写入基线：{BASELINE_PATH}")

    print(f"[evals] mode={args.mode} 用例={metrics.get('n_cases')} "
          f"任务成功率={_fmt(metrics.get('task_success_rate'))} "
          f"工具准确={_fmt(metrics.get('tool_call_accuracy'))} "
          f"数字一致={_fmt(metrics.get('number_fidelity'))} "
          f"守卫捕获={_fmt(metrics.get('grounding_catch_rate'))} "
          f"耗时={payload['meta']['wall_seconds']}s")
    print(f"[evals] 报告：{prefix}-latest.md")
    for r in regressions:
        print(f"[evals] ⚠️ 退化 {r}", file=sys.stderr)

    if args.check_baseline and regressions:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
