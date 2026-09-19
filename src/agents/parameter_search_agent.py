"""参数寻优 Agent —— 让模型真正决定控制流的那一环。

为什么需要它
------------
调度主链路（负荷预测 → MILP → 需求响应 → 解释）是一张**固定的有向图**：控制流写死在
编排里，LLM 只在末端写文案。那是工作流，不是智能体。本模块补一个模型真的在做决策的
循环：提案约束参数 → 用**真实 MILP** 评价 → 读回收益与违反项 → 反思 → 再提案，
直到它自己判断"够了"（模型可主动输出 stop）或预算耗尽。

为什么这个任务适合当 agent
--------------------------
输出**可验证**。模型说"SOC 上限压到 82% 会更好"，评价它的不是另一个模型的 judgement，
而是 HiGHS 的 Optimal/Infeasible 与一个有物理量纲的净收益。于是"agent 比固定流程强多少"
是数字，不是感觉。

三条不妥协的护栏
----------------
1. **安全解优先于最优解**：关掉热约束（`include_thermal=False`）几乎总能多赚钱，
   但峰值温度会越过 55 ℃ 停机线。这类候选可以被**评价**、不可以被**选中**，
   除非调用方显式 `allow_unsafe_winner=True` 并知情。
2. **同分辨率比较**：24 / 48 / 96 点是三个**不同的优化问题**，净收益不可跨分辨率比
   （同一配置实测 1285.79 / 1279.09 / 1226.94 元）。搜索阶段用粗分辨率省钱，
   胜出者必须回到 96 点、与默认配置**同场同路**复验后才能写进结论。
3. **允许 agent 输**：复验没跑赢默认配置，报告就写"未跑赢"并采用默认。
   与本项目负荷预测模块"朴素基线优先"同一口径。

一个必须避开的坑
----------------
`CoordinatorAgent.run_daily_scheduling` 从当日 CSV 取 96 点价格/温度，而
`StorageOptimizationAgent.optimize` 用的是 `cfg.battery.num_steps`。若把 num_steps 改成
48 却仍喂 96 点数组，模型会**只取前 48 个点＝一天前 12 小时**，静默解一个错问题，
而且解得又快又"最优"。所以搜索阶段的评价不经过协调器，而是把当日三条曲线按目标分辨率
做块平均重采样（`_resample`），所有候选共用同一组输入序列。

运行
----
    python -m src.agents.parameter_search_agent --date 2024-07-30          # 启发式提案，无需 Key
    DEEPSEEK_API_KEY=sk-... python -m src.agents.parameter_search_agent --llm
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from src.utils import trace
from src.utils.config import CONFIG, SystemConfig
from src.utils.logger import get_logger

log = get_logger("parameter_search_agent")

# 寻优只动 MILP 的效率类输入约束。
KNOBS: Dict[str, Dict[str, Any]] = {
    "soc_min_pct": {"kind": "int", "low": 1, "high": 40,
                    "desc": "SOC 下限（%）。抬高减少可用容量，但避开低 SOC 高衰减区（系数 3.0）"},
    "soc_max_pct": {"kind": "int", "low": 50, "high": 100,
                    "desc": "SOC 上限（%）。压低可避开 0.8~1.0 区（SEI 膜增长快，系数 2.0）"},
    "rated_power_kw": {"kind": "int", "low": 200, "high": 1000,
                       "desc": "允许的最大充放电功率（kW）。降功率是最直接的省热手段"},
}
# 安全策略项：寻优**不许**提议，即使它能带来最高收益。
# 两个理由：① 热约束是安全边界不是成本旋钮，让优化器拿它换收益是范畴错误；
#          ② 搜索阶段跑在粗分辨率上，热峰值被系统性低估（24 点实测约 35 ℃，
#             96 点同配置约 47 ℃），粗分辨率根本"看不见"越温限的解。
# 人在界面上显式关掉热约束是一次明确的工程决策；优化器替人做这个决定不是。
POLICY_KNOBS: Tuple[str, ...] = ("include_thermal", "enable_dr")
MIN_SOC_GAP_PCT = 10          # 上下限至少留 10%，否则可用容量近乎归零
ENERGY_BALANCE_TOL_KWH = 1.0  # 后验能量守恒残差容差
# 求解器在 mip_gap=1% 下允许提前停在可行解，同一配置连跑两次实测能差出 ~1%
# （24 点默认约束三次实测：1244.24 / 1255.87 / 1260.58 元，与
#  docs/parameter-search-sample.md 候选轨迹第 1 行的默认配置 1260.58 同源）。
#  所以"更小即更优"的比较必须有实质增幅，否则寻优会去追求解器抖动。取 2% > mip_gap，留一倍余量。
MATERIAL_GAIN_RATIO = 0.02


# ============================================================================
# 候选解
# ============================================================================
@dataclass
class Candidate:
    """一次求解的完整结果，含"能不能要"的独立判据。"""
    params: Dict[str, Any] = field(default_factory=dict)
    net_revenue_yuan: float = 0.0
    arbitrage_yuan: float = 0.0
    degradation_yuan: float = 0.0
    max_temp_c: float = 0.0
    solver_status: str = ""
    soc_violation_steps: int = 0
    energy_balance_err_kwh: float = 0.0
    equivalent_cycles: float = 0.0
    source: str = "default"
    stage: str = "search"
    solve_ms: float = 0.0
    reject_reasons: List[str] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        """工程可行性（不含安全越限）。"""
        return not [r for r in self.reject_reasons if not r.startswith(_UNSAFE_PREFIX)]

    @property
    def safe(self) -> bool:
        return not any(r.startswith(_UNSAFE_PREFIX) for r in self.reject_reasons)

    @property
    def selectable(self) -> bool:
        return self.feasible and self.safe

    def brief(self) -> str:
        return (", ".join(f"{k}={v}" for k, v in sorted(self.params.items()))
                or "（默认配置）")


_UNSAFE_PREFIX = "unsafe:"


@dataclass
class SearchReport:
    """一次寻优的结论。to_markdown() 可直接落文档。"""
    date: str
    candidates: List[Candidate] = field(default_factory=list)
    winner: Optional[Candidate] = None
    verified_default: Optional[Candidate] = None
    verified_winner: Optional[Candidate] = None
    improvement_yuan: float = 0.0
    improvement_pct: float = 0.0
    agent_won: bool = False
    verdict: str = ""
    search_steps: int = 0
    verify_steps: int = 0
    budget_steps: int = 0
    used_steps: int = 0
    stopped_by: str = ""
    rejected_infeasible: int = 0
    rejected_unsafe: int = 0
    out_of_bounds: int = 0
    duplicate_proposals: int = 0
    llm_calls: int = 0
    llm_fallbacks: int = 0
    elapsed_s: float = 0.0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "agent_won": self.agent_won,
            "winner_params": self.winner.params if self.winner else None,
            "verified": {
                "default_yuan": (round(self.verified_default.net_revenue_yuan, 2)
                                 if self.verified_default else None),
                "winner_yuan": (round(self.verified_winner.net_revenue_yuan, 2)
                                if self.verified_winner else None),
                "resolution_points": self.verify_steps,
            },
            "improvement_yuan": round(self.improvement_yuan, 2),
            "improvement_pct": round(self.improvement_pct, 2),
            "stopped_by": self.stopped_by,
            "used_steps": self.used_steps,
            "rejected": {"infeasible": self.rejected_infeasible,
                         "unsafe": self.rejected_unsafe,
                         "out_of_bounds": self.out_of_bounds,
                         "duplicate": self.duplicate_proposals},
            "llm": {"calls": self.llm_calls, "fallbacks": self.llm_fallbacks},
            "resolution": {"search_points": self.search_steps,
                           "verify_points": self.verify_steps},
            "elapsed_s": round(self.elapsed_s, 1),
            "notes": self.notes,
            "candidates": [
                {"params": c.params, "source": c.source, "stage": c.stage,
                 "net_revenue_yuan": round(c.net_revenue_yuan, 2),
                 "max_temp_c": round(c.max_temp_c, 2),
                 "solver_status": c.solver_status,
                 "feasible": c.feasible, "safe": c.safe,
                 "reject_reasons": c.reject_reasons}
                for c in self.candidates],
        }

    def to_markdown(self) -> str:
        lines = [
            f"# 参数寻优 Agent 报告（{self.date}）",
            "",
            f"- 搜索分辨率 {self.search_steps} 点 → 复验分辨率 {self.verify_steps} 点。"
            "跨分辨率净收益不可比（护栏 2），结论只用下表的同场复验值。",
            f"- 提案 {self.used_steps}/{self.budget_steps} 步后停止（{self.stopped_by}）；"
            f"被拒：不可行 {self.rejected_infeasible}、不安全 {self.rejected_unsafe}、"
            f"越界 {self.out_of_bounds}、重复 {self.duplicate_proposals}",
            f"- LLM 调用 {self.llm_calls} 次（回退启发式 {self.llm_fallbacks} 次）"
            f"｜耗时 {self.elapsed_s:.1f} s",
            "",
            "## 同分辨率复验（唯一可比的两个数）",
            "",
            "| 配置 | 净收益(元) | 最高温(℃) | 等效循环 | 求解状态 |",
            "|---|---|---|---|---|",
        ]
        for label, cand in (("固定流水线（默认约束）", self.verified_default),
                            ("Agent 胜出约束", self.verified_winner)):
            if cand is None:
                continue
            lines.append(f"| {label} `{cand.brief()}` | {cand.net_revenue_yuan:.2f} | "
                         f"{cand.max_temp_c:.1f} | {cand.equivalent_cycles:.4f} | "
                         f"`{cand.solver_status}` |")
        lines += ["", f"**结论：{self.verdict or '（寻优未产生结论，检查是否走到了终止分支）'}**", ""]
        lines += ["", "## 候选轨迹", "",
                  "| # | 阶段 | 来源 | 参数 | 净收益(元) | 最高温(℃) | 可选 | 拒绝原因 |",
                  "|---|---|---|---|---|---|---|---|"]
        for i, c in enumerate(self.candidates, 1):
            lines.append(f"| {i} | {c.stage} | {c.source} | `{c.brief()}` | "
                         f"{c.net_revenue_yuan:.2f} | {c.max_temp_c:.1f} | "
                         f"{'✅' if c.selectable else '❌'} | "
                         f"{'；'.join(c.reject_reasons) or '—'} |")
        if self.notes:
            lines += ["", "## 备注", ""] + [f"- {n}" for n in self.notes]
        return "\n".join(lines) + "\n"


# ============================================================================
# 提案器
# ============================================================================
def clamp_params(params: Dict[str, Any], cfg: SystemConfig
                 ) -> Tuple[Optional[Dict[str, Any]], str]:
    """校验并归一化提案。返回 (归一化后的参数或 None, 拒收原因)。

    越界提案一律不交给求解器：宁可拒收，也不要"静默按错误约束解一个最优"。
    """
    out = dict(params)
    for key in ("soc_min_pct", "soc_max_pct", "rated_power_kw"):
        if key in out:
            try:
                out[key] = int(out[key])
            except (TypeError, ValueError):
                return None, f"{key} 必须是整数，收到 {out[key]!r}"
    for key in POLICY_KNOBS:
        if key in out:
            return None, (f"{key} 是安全策略项，不参与寻优"
                          "（要改请在界面/接口上显式修改，那是人的决定）")
    unknown = set(out) - set(KNOBS)
    if unknown:
        return None, f"未知旋钮 {sorted(unknown)}"

    # 先查内部一致性再查边界：SOC 上下限的边界（1~40 / 50~100）本身已保证窗口
    # 不会过窄，但边界一旦放宽，这条就得先说话，否则"窗口近乎归零"会被"越界"掩盖。
    lo = out.get("soc_min_pct", int(round(cfg.battery.soc_min * 100)))
    hi = out.get("soc_max_pct", int(round(cfg.battery.soc_max * 100)))
    if hi - lo < MIN_SOC_GAP_PCT:
        return None, f"SOC 窗口过窄（{lo}%~{hi}%，差 < {MIN_SOC_GAP_PCT}%），可用容量近乎归零"

    for key, spec in KNOBS.items():
        if key in out and spec["kind"] == "int":
            if not (spec["low"] <= out[key] <= spec["high"]):
                return None, f"{key}={out[key]} 越界（应在 {spec['low']}~{spec['high']}）"
    return out, ""


class HeuristicProposer:
    """确定性坐标下降：一次只动一个旋钮，必要时组合两个。

    它存在的理由不是替代模型，而是**提供一条不带模型也能跑的非智能基线**——
    否则"agent 有效"这句话里没有对照物。它也保证无 Key / 断网 / 模型抖一下时
    寻优照样能跑完（LLMProposer 的回退目标就是它）。
    """
    mode = "heuristic"

    def __init__(self) -> None:
        self._queue: List[Dict[str, Any]] = []
        self._planned = False

    def reset(self) -> None:
        self._queue = []
        self._planned = False

    def propose(self, history: Sequence[Candidate], cfg: SystemConfig) -> Optional[Dict[str, Any]]:
        tried = {_key(c.params) for c in history}
        if not self._planned:
            self._plan(cfg)
            self._planned = True
        while self._queue:
            cand = self._queue.pop(0)
            if _key(cand) not in tried:
                return cand
        return None

    def _plan(self, cfg: SystemConfig) -> None:
        base_min = int(round(cfg.battery.soc_min * 100))
        base_max = int(round(cfg.battery.soc_max * 100))
        base_p = int(round(cfg.battery.rated_power_kw))
        # 顺序有意为之：先试散热（降功率），再收 SOC 窗口。
        # 不含 include_thermal：那是安全策略项，见 POLICY_KNOBS。
        self._queue = [
            {"rated_power_kw": int(base_p * 0.7)},
            {"soc_max_pct": base_max - 10},
            {"rated_power_kw": int(base_p * 0.5)},
            {"soc_max_pct": base_max - 20},
            {"soc_min_pct": base_min + 10},
            {"soc_max_pct": base_max - 10, "rated_power_kw": int(base_p * 0.7)},
        ]


def _key(params: Dict[str, Any]) -> str:
    return json.dumps(params, sort_keys=True, ensure_ascii=False)


class LLMProposer:
    """让模型读候选轨迹，决定下一步试什么，并可以主动喊停。

    任何一环不对（无 Key、超时、JSON 解析失败、字段越界）都回落启发式并计一次
    fallback：寻优不该因为模型抖一下就断在半路。
    """
    mode = "llm"

    SYSTEM_PROMPT = """你是工商业储能调度约束参数寻优助手。

你会看到：可动旋钮及其边界、已试过的约束组合与它们的**真实 MILP 求解结果**
（净收益、峰值温度、求解状态、被拒原因）。这些数字来自求解器，不是估算。

任务：决定下一步试哪一组参数，或判断继续试下去不值得（stop）。

规则：
1. 只输出一个 JSON 对象，不要解释、不要代码块围栏。
2. 键只能来自给定旋钮名，值必须在边界内；一次最多改 2 个旋钮，便于归因。
3. 峰值温度越过 55℃ 停机线的解不可取，即使净收益更高。
4. 只能提议 soc_min_pct / soc_max_pct / rated_power_kw。热约束是安全策略项，
   提议 include_thermal 会被直接拒收，不要试图用收益换它。
5. 若最近的改动都没有提升，输出 {"action": "stop", "reason": "一句话说明"}。

格式：{"action": "try", "params": {...}, "reason": "一句话假设"}"""

    def __init__(self, api_key: str = "", base_url: str = "", model: str = "",
                 client: Optional[Callable[..., str]] = None,
                 fallback: Optional[HeuristicProposer] = None) -> None:
        from src.agents.llm_explainer import resolve_llm_credentials
        self.api_key, self.base_url, self.model = resolve_llm_credentials(
            api_key, base_url, model)
        self._client = client
        self.fallback = fallback or HeuristicProposer()
        self.calls = 0
        self.fallbacks = 0
        self.last_stop_reason = ""

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def reset(self) -> None:
        self.fallback.reset()
        self.last_stop_reason = ""

    def _user_prompt(self, history: Sequence[Candidate], cfg: SystemConfig) -> str:
        knobs = [f"- {name}：{spec['desc']}；边界 {spec['low']}~{spec['high']}"
                 for name, spec in KNOBS.items()]
        knobs.append("- 不可提议：include_thermal / enable_dr（安全与结算策略项，提议即拒收）")
        rows = ["| 参数 | 净收益(元) | 峰值温度(℃) | 状态 | 结论 |",
                "|---|---|---|---|---|"]
        for c in history:
            verdict = "可选" if c.selectable else "不可选：" + "；".join(c.reject_reasons)
            rows.append(f"| `{c.brief()}` | {c.net_revenue_yuan:.2f} | {c.max_temp_c:.1f} | "
                        f"{c.solver_status} | {verdict} |")
        return ("\n".join(knobs) + "\n\n已试过的候选（按时间顺序）：\n"
                + "\n".join(rows) + "\n\n请给出下一步动作的 JSON。")

    def _call(self, messages: List[Dict[str, str]]) -> str:
        if self._client is not None:
            return self._client(messages=messages, api_key=self.api_key,
                                base_url=self.base_url, model=self.model)
        from src.agents.llm_explainer import call_chat_completions
        return call_chat_completions(messages=messages, api_key=self.api_key,
                                     base_url=self.base_url, model=self.model,
                                     temperature=0.0)

    @staticmethod
    def parse(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """从模型输出里取动作。返回 (参数或 None, stop 原因)。"""
        match = re.search(r"\{.*\}", text or "", re.S)
        if not match:
            return None, ""
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None, ""
        if not isinstance(obj, dict):
            return None, ""
        if str(obj.get("action", "")).lower() == "stop":
            return None, str(obj.get("reason") or "模型判断继续寻优不划算")
        params = obj.get("params")
        if not isinstance(params, dict) or not params:
            return None, ""
        return params, ""

    def propose(self, history: Sequence[Candidate], cfg: SystemConfig
                ) -> Optional[Dict[str, Any]]:
        """返回下一组参数；None 表示"停"。"""
        if not self.enabled:
            self.fallbacks += 1
            return self.fallback.propose(history, cfg)
        self.calls += 1
        try:
            answer = self._call([{"role": "system", "content": self.SYSTEM_PROMPT},
                                 {"role": "user",
                                  "content": self._user_prompt(history, cfg)}])
        except Exception as exc:
            log.warning("寻优 LLM 调用失败（%s），本步回退启发式提案", exc)
            self.fallbacks += 1
            return self.fallback.propose(history, cfg)

        params, stop_reason = self.parse(answer)
        if stop_reason:
            self.last_stop_reason = stop_reason
            return None
        if params is None:
            log.warning("寻优 LLM 输出无法解析为动作 JSON（%s），本步回退启发式",
                        (answer or "")[:120])
            self.fallbacks += 1
            return self.fallback.propose(history, cfg)
        checked, why = clamp_params(params, cfg)
        if checked is None:
            log.warning("寻优 LLM 提案被拒（%s），本步回退启发式", why)
            self.fallbacks += 1
            return self.fallback.propose(history, cfg)
        return checked


def _common_notes() -> List[str]:
    """两条口径说明在"复验了"和"没复验"两个出口都要出现，否则报告读起来会缺前提。"""
    return [
        "搜索阶段净收益不含需求响应补贴（storage-only 口径），复验阶段走完整生产链路"
        "含 DR；两个数不可互相比较。",
        f"胜出门槛：净收益增幅须 > {MATERIAL_GAIN_RATIO:.0%}。mip_gap=1% 下同配置重复"
        f"求解实测可抖动约 1%（24 点默认约束三次跑出 1244.24 / 1255.87 / 1260.58 元），"
        f"小于门槛的差异是求解器噪声，不认作战果。",
    ]


# ============================================================================
# 寻优 Agent
# ============================================================================
def _resample(series: np.ndarray, num_points: int) -> np.ndarray:
    """96 点 → num_points 点的块平均。分辨率变了，输入序列必须跟着变。"""
    arr = np.asarray(series, dtype=float)
    if num_points <= 0 or len(arr) % num_points:
        raise ValueError(f"序列长度 {len(arr)} 无法整除目标点数 {num_points}")
    return arr.reshape(num_points, len(arr) // num_points).mean(axis=1)


class ParameterSearchAgent:
    """提案 → 真实求解 → 独立校验 → 再提案的闭环。"""

    def __init__(self, config: Optional[SystemConfig] = None,
                 proposer: Optional[Any] = None,
                 evaluate: Optional[Callable[[Dict[str, Any], int], Candidate]] = None,
                 verify_eval: Optional[Callable[[Dict[str, Any], int], Candidate]] = None,
                 max_steps: int = 6, deadline_s: float = 180.0,
                 search_steps: int = 48, verify_steps: int = 96,
                 allow_unsafe_winner: bool = False) -> None:
        self.cfg = config or CONFIG
        self.proposer = proposer if proposer is not None else HeuristicProposer()
        self._injected_eval = evaluate
        # 复验单独可注入：它是唯一走完整生产链路（含 DR）的环节，96 点一次约 48 秒。
        # 不给这个注入口，单元测试就只能要么真跑两遍 MILP、要么跳过复验分支——
        # 而复验恰恰是"agent 到底有没有跑赢"结论的来源，必须被测到。
        self._injected_verify = verify_eval
        self.max_steps = max_steps
        self.deadline_s = deadline_s
        self.search_steps = search_steps
        self.verify_steps = verify_steps
        self.allow_unsafe_winner = allow_unsafe_winner
        self._day_cache: Dict[int, Dict[str, Any]] = {}

    # ---------- 输入准备（按分辨率重采样） ----------
    def _day_series(self, date: str, historical_data: Any, num_points: int) -> Dict[str, Any]:
        import pandas as pd
        cached = self._day_cache.get(num_points)
        if cached is not None and cached.get("date") == date:
            return cached
        import src.data.data_generator as dg
        day = pd.date_range(date, periods=96, freq="15min")
        price = np.asarray(dg.generate_price_profile(day), dtype=float)
        amb = np.asarray(dg.generate_ambient_temp(day), dtype=float)
        rows = historical_data[
            historical_data["timestamp"].dt.date == pd.to_datetime(date).date()]
        load = (np.asarray(rows["load_kw"].values, dtype=float) if len(rows) == 96
                else np.asarray(dg.generate_load_profile(day), dtype=float))
        out = {"date": date,
               "price": _resample(price, num_points),
               "ambient": _resample(amb, num_points),
               "load": _resample(load, num_points)}
        self._day_cache[num_points] = out
        return out

    # ---------- 搜索阶段评价：storage-only MILP ----------
    def evaluate(self, params: Dict[str, Any], num_points: int, date: str,
                 historical_data: Any, source: str = "") -> Candidate:
        from src.agents.storage_optimization_agent import StorageOptimizationAgent

        battery = self.cfg.battery
        override: Dict[str, Any] = {"num_steps": num_points,
                                    "time_step_hours": round(24.0 / num_points, 6)}
        if params.get("soc_min_pct") is not None:
            override["soc_min"] = float(params["soc_min_pct"]) / 100.0
        if params.get("soc_max_pct") is not None:
            override["soc_max"] = float(params["soc_max_pct"]) / 100.0
        if params.get("rated_power_kw") is not None:
            override["rated_power_kw"] = float(params["rated_power_kw"])
        cfg = replace(self.cfg, battery=replace(battery, **override))

        series = self._day_series(date, historical_data, num_points)
        # 热约束恒为真：它是安全策略项，不参与寻优（见 POLICY_KNOBS）
        started = time.perf_counter()
        sched = StorageOptimizationAgent(cfg).optimize(
            price_profile=series["price"], load_profile=series["load"],
            ambient_temp_profile=series["ambient"],
            include_thermal_constraint=True,
            include_degradation_cost=True)
        solve_ms = (time.perf_counter() - started) * 1000.0

        cand = Candidate(
            params=dict(params), source=source or getattr(self.proposer, "mode", "?"),
            stage="search", solve_ms=solve_ms,
            net_revenue_yuan=float(sched.net_revenue_yuan),
            arbitrage_yuan=float(sched.arbitrage_revenue_yuan),
            degradation_yuan=float(sched.degradation_cost_yuan),
            max_temp_c=float(sched.max_battery_temp_c),
            solver_status=str(sched.solver_status),
            soc_violation_steps=int(sched.soc_violation_steps),
            energy_balance_err_kwh=float(sched.energy_balance_error_kwh),
            equivalent_cycles=float(sched.equivalent_cycles))
        cand.reject_reasons = self._rejections(cand, cfg)
        return cand

    # ---------- 复验阶段评价：完整生产链路（含需求响应） ----------
    def verify(self, params: Dict[str, Any], num_points: int, date: str,
               historical_data: Any, dr_signals: Sequence[Any],
               source: str = "verify") -> Candidate:
        from src.agents.coordinator_agent import CoordinatorAgent

        battery = self.cfg.battery
        if num_points != battery.num_steps:
            raise ValueError(
                f"复验必须跑在 {battery.num_steps} 点（与当日 CSV 数据同分辨率）。"
                f"协调器按 CSV 取 96 点价格/温度，把 num_steps 改成 {num_points} 会让 "
                "MILP 只取前一半时段的数组，静默解一个错问题（正是本模块要避开的坑）。")
        override: Dict[str, Any] = {}
        if params.get("soc_min_pct") is not None:
            override["soc_min"] = float(params["soc_min_pct"]) / 100.0
        if params.get("soc_max_pct") is not None:
            override["soc_max"] = float(params["soc_max_pct"]) / 100.0
        if params.get("rated_power_kw") is not None:
            override["rated_power_kw"] = float(params["rated_power_kw"])
        cfg = replace(self.cfg, battery=replace(battery, **override)) if override else self.cfg

        started = time.perf_counter()
        coord = CoordinatorAgent(cfg)
        report = coord.run_daily_scheduling(
            date=date, historical_data=historical_data, dr_signals=list(dr_signals),
            use_ml_forecast=False,          # 寻优要的是可比性，不是预测精度
            include_thermal=True,           # 安全策略项，不由寻优决定
            include_degradation=True)
        solve_ms = (time.perf_counter() - started) * 1000.0
        final = coord.state.final_schedule

        cand = Candidate(
            params=dict(params), source=source, stage="verify", solve_ms=solve_ms,
            net_revenue_yuan=float(report.net_revenue_yuan),
            arbitrage_yuan=float(report.arbitrage_revenue_yuan),
            degradation_yuan=float(report.degradation_cost_yuan),
            max_temp_c=float(report.max_battery_temp_c),
            solver_status=str(getattr(final, "solver_status", "")),
            soc_violation_steps=int(getattr(final, "soc_violation_steps", 0)),
            energy_balance_err_kwh=float(getattr(final, "energy_balance_error_kwh", 0.0)),
            equivalent_cycles=float(report.equivalent_cycles))
        cand.reject_reasons = self._rejections(cand, cfg)
        return cand

    # 状态字符串在本项目里有多种来源：PuLP 的 Optimal / Infeasible / Undefined /
    # Not Solved / "Stopped (time limit, feasible incumbent)"，以及 DR 改写后的
    # Final_DR_Adjusted、基线的 Baseline。所以只能按"已知不可行"判，不能用
    # "包含 feasible 才算可行" —— "Infeasible" 本身就含 "feasible"，会把不可行解放过去。
    INFEASIBLE_STATUS = ("infeasible", "undefined", "not solved", "no solver")

    @staticmethod
    def _status_infeasible(status: str) -> bool:
        low = (status or "").strip().lower()
        if not low:
            return True
        if any(b in low for b in ParameterSearchAgent.INFEASIBLE_STATUS):
            return True
        return "stopped" in low and "feasible" not in low

    @staticmethod
    def _rejections(cand: Candidate, cfg: SystemConfig) -> List[str]:
        """可行性与安全性在**解出来之后**独立复核，不采信求解器的自我声明。"""
        reasons: List[str] = []
        if ParameterSearchAgent._status_infeasible(cand.solver_status):
            reasons.append(f"求解状态不可行（{cand.solver_status or '空'}）")
        if cand.soc_violation_steps:
            reasons.append(f"SOC 越界 {cand.soc_violation_steps} 步")
        if abs(cand.energy_balance_err_kwh) > ENERGY_BALANCE_TOL_KWH:
            reasons.append(f"能量守恒残差 {cand.energy_balance_err_kwh:.2f} kWh 超限")
        if cand.net_revenue_yuan < 0:
            reasons.append(f"净收益为负（{cand.net_revenue_yuan:.2f} 元）")
        # 只有越过停机线才算不安全；进入 45~55℃ 降额区是允许的运行工况
        # （见 README 的温度保护策略表），不作为拒绝理由。
        if cand.max_temp_c > cfg.battery.temp_safe_max:
            reasons.append(f"{_UNSAFE_PREFIX} 峰值温度 {cand.max_temp_c:.1f}℃ 越过 "
                           f"{cfg.battery.temp_safe_max:.0f}℃ 停机线")
        return reasons

    # ---------- 主循环 ----------
    @trace.traced("param_search")
    def run(self, date: str, historical_data: Any = None,
            dr_signals: Optional[Sequence[Any]] = None) -> SearchReport:
        from src.data.data_loader import load_load_data

        t0 = time.perf_counter()
        hist = historical_data if historical_data is not None else load_load_data()
        drs = list(dr_signals or [])
        evaluate = self._injected_eval or (
            lambda params, steps: self.evaluate(params, steps, date, hist))

        report = SearchReport(date=date, search_steps=self.search_steps,
                              verify_steps=self.verify_steps, budget_steps=self.max_steps)

        default = evaluate({}, self.search_steps)
        default.source, default.stage = "default", "search"
        report.candidates.append(default)
        if not default.selectable:
            report.notes.append("默认配置本身就不可行：先查输入数据与求解器，寻优无意义。")
            report.verdict = "默认配置不可行，寻优无意义——先查输入数据与求解器。"
            report.stopped_by = "default_infeasible"
            report.elapsed_s = time.perf_counter() - t0
            return report

        best: Candidate = default
        seen = {_key(c.params) for c in report.candidates}
        if hasattr(self.proposer, "reset"):
            self.proposer.reset()

        stopped_by = "budget"
        for step in range(self.max_steps):
            if time.perf_counter() - t0 > self.deadline_s:
                stopped_by = "deadline"
                break
            proposal = self.proposer.propose(report.candidates, self.cfg)
            if proposal is None:
                stopped_by = "proposer_stop"
                break
            checked, why = clamp_params(proposal, self.cfg)
            if checked is None:
                # 越界不消耗求解预算，但也不中断寻优：记一笔接着试下一案。
                report.out_of_bounds += 1
                log.warning("第 %d 步提案被拒（%s），不入库、继续", step + 1, why)
                continue
            if _key(checked) in seen:
                # 重复提案直接吃掉一次 0.4~48 秒的求解：模型连发两次同一组参数时，
                # 预算应花在没试过的组合上，而不是同一组上。
                report.duplicate_proposals += 1
                log.warning("第 %d 步提案与已试过组合重复（%s），跳过求解", step + 1,
                            _key(checked))
                continue
            seen.add(_key(checked))
            with trace.span("evaluate", resolution=self.search_steps):
                cand = evaluate(checked, self.search_steps)
            cand.source = getattr(self.proposer, "mode", "heuristic")
            report.candidates.append(cand)
            report.used_steps = step + 1
            if not cand.feasible:
                report.rejected_infeasible += 1
                continue
            if not cand.safe:
                report.rejected_unsafe += 1
                if not self.allow_unsafe_winner:
                    continue
            margin = max(1.0, abs(best.net_revenue_yuan) * MATERIAL_GAIN_RATIO)
            if cand.net_revenue_yuan > best.net_revenue_yuan + margin:
                best = cand
        report.stopped_by = stopped_by

        if hasattr(self.proposer, "calls"):
            report.llm_calls = int(getattr(self.proposer, "calls", 0))
            report.llm_fallbacks = int(getattr(self.proposer, "fallbacks", 0))

        if not best.params:
            report.winner = None
            report.verdict = (f"搜索阶段（{self.search_steps} 点）未找到优于默认约束的可选项，"
                              "保持默认配置；两组粗分辨率数字都不进结论，故不复验。")
            report.notes.extend(_common_notes())
            report.elapsed_s = time.perf_counter() - t0
            return report

        report.winner = best
        # 同分辨率、同路径复验：结论只用这两个数（护栏 2）。
        verify = self._injected_verify or (
            lambda params, steps: self.verify(params, steps, date, hist, drs))
        report.verified_default = verify({}, self.verify_steps)
        report.verified_default.source = "default"
        report.verified_default.stage = "verify"
        report.verified_winner = verify(best.params, self.verify_steps)
        report.verified_winner.stage = "verify"
        base = report.verified_default.net_revenue_yuan
        win = report.verified_winner.net_revenue_yuan
        report.improvement_yuan = win - base
        report.improvement_pct = (win - base) / abs(base) * 100.0 if base else 0.0
        need = max(1.0, abs(base) * MATERIAL_GAIN_RATIO)
        report.agent_won = bool(report.verified_winner.selectable and win > base + need)
        if report.agent_won:
            report.verdict = (f"agent 跑赢固定流水线 {report.improvement_yuan:+.2f} 元"
                              f"（{report.improvement_pct:+.1f}%），采用胜出约束 "
                              f"`{best.brief()}`")
        else:
            report.verdict = (f"agent **未跑赢**固定流水线：同 {self.verify_steps} 点复验 "
                              f"{base:.2f} → {win:.2f} 元，采用默认配置")
            report.notes.append("agent 未跑赢固定流水线（同分辨率复验后），"
                                "最终采用默认配置——与负荷预测模块"
                                "「朴素基线优先」同一口径。")
        report.notes.extend(_common_notes())
        report.elapsed_s = time.perf_counter() - t0
        rt = trace.current_run()
        if rt is not None:
            rt.attrs.update(candidates=len(report.candidates), used_steps=report.used_steps,
                             stopped_by=report.stopped_by, agent_won=report.agent_won)
        return report


# ============================================================================
# CLI
# ============================================================================
def _load_dr_signals(date: str) -> List[Any]:
    """取当日的需求响应事件（CSV 列名是 type，不是 dr_type）。"""
    from src.agents.demand_response_agent import DRSignal
    from src.data.data_loader import load_dr_signals

    frame = load_dr_signals()
    out: List[Any] = []
    for row in frame.to_dict("records"):
        start = str(row.get("start_time", ""))
        if not start.startswith(date):
            continue
        out.append(DRSignal(
            start_time=start, end_time=str(row.get("end_time")),
            target_reduction_kw=float(row.get("target_reduction_kw", 0.0)),
            subsidy_per_kwh=float(row.get("subsidy_per_kwh", 0.8)),
            dr_type=str(row.get("type") or row.get("dr_type") or "peak_shaving")))
    return out


def _utf8_stdout() -> None:
    """Windows 控制台默认 GBK，报告里的 ✅/℃/元 会直接 UnicodeEncodeError。

    开发与实测环境就是 Windows 10，这条不是锦上添花：不加，CLI 在目标平台上跑不通。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):    # 被重定向到管道等场景可能不支持
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(description="储能调度约束参数寻优 Agent")
    ap.add_argument("--date", default="2024-07-30")
    ap.add_argument("--llm", action="store_true", help="用模型提案（凭据走环境变量）")
    ap.add_argument("--max-steps", type=int, default=6)
    ap.add_argument("--search-steps", type=int, default=48, help="搜索分辨率（点数）")
    ap.add_argument("--verify-steps", type=int, default=96, help="复验分辨率（点数）")
    ap.add_argument("--with-dr", action="store_true",
                    help="复验阶段携带当日需求响应事件（净收益含 DR 补贴）")
    ap.add_argument("--out", default="", help="报告另存为 markdown")
    args = ap.parse_args(list(argv) if argv else None)

    proposer = LLMProposer() if args.llm else HeuristicProposer()
    if args.llm and not getattr(proposer, "enabled", False):
        print("提示：未检测到 API Key，逐步回退启发式提案。", file=sys.stderr)

    agent = ParameterSearchAgent(proposer=proposer, max_steps=args.max_steps,
                                 search_steps=args.search_steps,
                                 verify_steps=args.verify_steps)
    drs = _load_dr_signals(args.date) if args.with_dr else []
    report = agent.run(args.date, None, drs)
    md = report.to_markdown()
    print(md)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"已保存：{args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
