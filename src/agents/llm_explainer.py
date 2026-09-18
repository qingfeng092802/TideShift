"""
LLM 决策解释层

定位：把 MILP 求解 + 后验仿真产生的**硬数字**压缩成一份"事实摘要"(DecisionDigest)，
再交给 LLM 生成人类可读的决策解释。LLM 只负责组织语言，不负责算数。

为什么这么设计：

1. **LLM 不碰计算**：优化结果由 MILP 给出，热/衰减由物理模型后验给出。
   LLM 拿到的只是已经算完的数字，从源头上杜绝"模型自己编一个收益"。
2. **事实摘要是唯一事实来源**：提示词里明确"只能使用摘要中的数字"。
3. **输出侧防幻觉校验**：`check_grounding()` 把回答里带单位的数字抽出来，
   逐条回查摘要；对不上号的数字会被标记并附在回答末尾，而不是默默放行。

降级策略：没有 API Key / 调用失败 / 超时，一律走 `template_explain()` 规则模板，
保证无网无 Key 也能演示，且模板输出与 LLM 输出结构一致。

零新增依赖：HTTP 调用走标准库 urllib（OpenAI 兼容 /chat/completions），
不引入 openai / langchain 版本依赖，测试可直接 monkeypatch。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from src.utils import trace

import numpy as np

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT_S = 60


class LLMError(RuntimeError):
    """LLM 调用失败（网络/鉴权/超时），调用方应降级到规则模板"""


# ========== 凭据解析 ==========
# LangGraph 的 explanation_node 以 `LLMExplainer()` 无参构造，拿不到 Web 界面
# 配置的 provider 凭据，只能回退环境变量——而服务进程内并无该环境变量，导致调度内嵌的
# 「LLM 决策解释」长期静默走规则模板（日志 source=rule），界面配好的 Key 只对对话 Agent
# 生效。此处提供进程级默认凭据，由 backend/server.py 在加载/保存模型配置后注入。
_DEFAULT_CREDS: Dict[str, Optional[str]] = {"api_key": None, "base_url": None, "model": None}


def set_default_credentials(api_key: Optional[str] = None,
                            base_url: Optional[str] = None,
                            model: Optional[str] = None) -> None:
    """注入进程级默认凭据（空串/None 表示该字段不启用）。

    仅影响未显式传参的调用点；显式传参依然优先。
    """
    _DEFAULT_CREDS["api_key"] = (api_key or "").strip() or None
    _DEFAULT_CREDS["base_url"] = (base_url or "").strip().rstrip("/") or None
    _DEFAULT_CREDS["model"] = (model or "").strip() or None


def resolve_llm_credentials(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> Tuple[str, str, str]:
    """
    解析 LLM 凭据。优先级：显式传参 > 进程默认 > 环境变量 > 内置默认。

    环境变量：DEEPSEEK_API_KEY > OPENAI_API_KEY > LLM_API_KEY
    """
    key = (api_key or _DEFAULT_CREDS["api_key"]
           or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY") or "")
    url = base_url or _DEFAULT_CREDS["base_url"] or os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL
    mdl = model or _DEFAULT_CREDS["model"] or os.getenv("LLM_MODEL") or DEFAULT_MODEL
    return key.strip(), url.rstrip("/"), mdl


# ========== 极薄 OpenAI 兼容客户端 ==========
def call_chat_completions(
    messages: List[Dict[str, str]],
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.2,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> str:
    """
    调用 OpenAI 兼容的 /chat/completions 接口，返回 assistant 的文本内容。

    用 urllib 而不是 SDK：少一个依赖，少一层版本坑，测试时 monkeypatch 这个函数即可。
    """
    if not api_key:
        raise LLMError("缺少 API Key")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    req = urllib.request.Request(
        url=f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LLMError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}") from e
    except Exception as e:  # 网络/超时/解析
        raise LLMError(f"{type(e).__name__}: {str(e)[:200]}") from e

    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"响应格式异常: {str(body)[:200]}") from e


# ========== 事实摘要 ==========
def _hhmm(step_idx: int) -> str:
    """步索引 → HH:MM（15分钟粒度，96步=24h）"""
    minutes = int(step_idx * 15)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def summarize_windows(
    net_power: Sequence[float],
    price: Optional[Sequence[float]] = None,
    dt_hours: float = 0.25,
    min_power_kw: float = 50.0,
    top_k: int = 5,
) -> Tuple[List[str], List[str]]:
    """
    把 96 点功率曲线合并成"连续充/放电窗口"，按电量排序取前 top_k 个。

    返回 (充电窗口列表, 放电窗口列表)，每个元素是一行人类可读描述。
    这样 LLM 拿到的是"02:00-06:00 充电 1000kW"而不是 96 个裸数字，
    既省 token 又降低它乱读数的概率。
    """
    p = np.asarray(net_power, dtype=float)
    if price is None:
        price = np.zeros_like(p)
    pr = np.asarray(price, dtype=float)

    windows: List[Dict] = []
    cur_sign = 0
    start = 0
    for i in range(len(p) + 1):
        sign = 0 if i >= len(p) else (1 if p[i] >= min_power_kw else (-1 if p[i] <= -min_power_kw else 0))
        if sign != cur_sign:
            if cur_sign != 0 and i > start:
                seg = p[start:i]
                seg_price = pr[start:i]
                windows.append({
                    "sign": cur_sign,
                    "start": start,
                    "end": i,
                    "avg_power": float(np.mean(np.abs(seg))),
                    "energy": float(np.sum(np.abs(seg)) * dt_hours),
                    "avg_price": float(np.mean(seg_price)),
                })
            cur_sign = sign
            start = i

    def fmt(w: Dict) -> str:
        verb = "放电" if w["sign"] > 0 else "充电"
        return (f"{_hhmm(w['start'])}-{_hhmm(w['end'])} {verb} "
                f"平均{w['avg_power']:.0f}kW，电量{w['energy']:.0f}kWh，"
                f"均价{w['avg_price']:.2f}元/kWh")

    charge = sorted([w for w in windows if w["sign"] < 0], key=lambda w: -w["energy"])[:top_k]
    discharge = sorted([w for w in windows if w["sign"] > 0], key=lambda w: -w["energy"])[:top_k]
    # 按时间排序输出，读起来是"一天里发生了什么"而不是"电量排名"
    charge.sort(key=lambda w: w["start"])
    discharge.sort(key=lambda w: w["start"])
    return [fmt(w) for w in charge], [fmt(w) for w in discharge]


@dataclass
class DecisionDigest:
    """一份"事实摘要"：LLM 能看到的所有数字都在这里，别处一概不许它自己造"""

    date: str = ""

    # 经济
    arbitrage_revenue_yuan: float = 0.0
    dr_subsidy_yuan: float = 0.0
    degradation_cost_yuan: float = 0.0
    net_revenue_yuan: float = 0.0

    # 物理
    charge_energy_kwh: float = 0.0
    discharge_energy_kwh: float = 0.0
    max_temp_c: float = 0.0
    avg_temp_c: float = 0.0
    temp_headroom_c: float = 0.0      # 距安全上限还有多少℃
    equivalent_cycles: float = 0.0
    terminal_soc: float = 0.0
    energy_balance_error_kwh: float = 0.0
    soc_violation_steps: int = 0

    # 决策
    charge_windows: List[str] = field(default_factory=list)
    discharge_windows: List[str] = field(default_factory=list)
    dr_events: List[str] = field(default_factory=list)

    # 对照（口径：剥离DR补贴，保证与基准策略同口径对比）
    baseline_net_yuan: float = 0.0
    baseline_net_without_dr_yuan: float = 0.0
    net_without_dr_yuan: float = 0.0
    improvement_pct: float = 0.0
    forecast_mape: float = 0.0
    annualized_yuan: float = 0.0

    # 元信息
    solver_status: str = ""
    alerts: List[str] = field(default_factory=list)

    def to_text(self) -> str:
        """渲染成给 LLM 看的 Markdown 事实清单"""
        lines = [
            f"# 储能调度事实摘要（{self.date}）",
            "",
            "## 经济结果",
            f"- 峰谷套利收益：{self.arbitrage_revenue_yuan:.2f} 元",
            f"- 需求响应补贴：{self.dr_subsidy_yuan:.2f} 元",
            f"- 电池衰减成本：{self.degradation_cost_yuan:.2f} 元",
            f"- 日净收益：{self.net_revenue_yuan:.2f} 元",
            f"- 年化参考（按365天）：{self.annualized_yuan:.0f} 元",
            "",
            "## 物理与安全",
            f"- 充电量：{self.charge_energy_kwh:.1f} kWh；放电量：{self.discharge_energy_kwh:.1f} kWh",
            f"- 最高电池温度：{self.max_temp_c:.1f} ℃；平均温度：{self.avg_temp_c:.1f} ℃",
            f"- 距安全上限（55.0℃）余量：{self.temp_headroom_c:.1f} ℃",
            f"- 等效循环：{self.equivalent_cycles:.4f} 次；终值SOC：{self.terminal_soc * 100:.1f}%",
            f"- 能量守恒残差：{self.energy_balance_error_kwh:.4f} kWh；SOC越界步数：{self.soc_violation_steps}",
            "",
            "## 充电窗口",
        ]
        lines += [f"- {w}" for w in self.charge_windows] or ["- 无"]
        lines += ["", "## 放电窗口"]
        lines += [f"- {w}" for w in self.discharge_windows] or ["- 无"]
        lines += ["", "## 需求响应"]
        lines += [f"- {d}" for d in self.dr_events] or ["- 无DR事件"]
        lines += [
            "",
            "## 与基准策略对照（同口径：均不含DR补贴）",
            f"- 基准策略（谷段充满+尖峰放完）净收益：{self.baseline_net_without_dr_yuan:.2f} 元",
            f"- 本方案不计DR补贴的净收益：{self.net_without_dr_yuan:.2f} 元",
            f"- 同口径提升：{self.improvement_pct:.1f}%",
            f"- 本方案含DR补贴的总净收益：{self.net_revenue_yuan:.2f} 元"
            f"（其中DR补贴 {self.dr_subsidy_yuan:.2f} 元，基准策略不参与DR）",
            f"- 负荷预测MAPE：{self.forecast_mape:.2f}%",
            "",
            "## 求解与告警",
            f"- 求解器状态：{self.solver_status}",
        ]
        lines += [f"- {a}" for a in self.alerts] or ["- 无告警"]
        return "\n".join(lines)

    def ground_truth_numbers(self) -> List[float]:
        """摘要里出现的全部数值，供防幻觉校验回查"""
        vals: List[float] = []
        for k, v in asdict(self).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(float(v))
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, str):
                        vals += [float(x) for x in re.findall(r"-?\d+\.?\d*", item)]
        vals += [55.0]  # 安全上限常量
        return vals


def build_digest(
    report,
    schedule,
    price_profile: Optional[Sequence[float]] = None,
    baseline=None,
    dr_responses: Optional[Sequence] = None,
    ambient_temp: Optional[Sequence[float]] = None,
    temp_safe_max: float = 55.0,
) -> DecisionDigest:
    """
    从调度结果构造事实摘要。

    report:    DailyReport
    schedule:  ScheduleResult
    baseline:  可选 ScheduleResult（用于算提升幅度）
    """
    net_power = np.asarray(schedule.net_power_kw, dtype=float)
    temps = np.asarray(schedule.battery_temp_c, dtype=float)
    price = np.asarray(price_profile, dtype=float) if price_profile is not None else np.zeros_like(net_power)

    chg, dis = summarize_windows(net_power, price)

    dr_lines: List[str] = []
    for i, dr in enumerate(dr_responses or []):
        if getattr(dr, "accepted", False):
            dr_lines.append(
                f"事件{i+1}：接受，响应{dr.response_power_kw:.0f}kW，"
                f"补贴{dr.subsidy_revenue_yuan:.2f}元，衰减{dr.degradation_cost_yuan:.2f}元，"
                f"净{dr.net_dr_revenue_yuan:.2f}元"
            )
        else:
            dr_lines.append(f"事件{i+1}：拒绝，原因：{getattr(dr, 'reason', '未知')}")

    baseline_net = float(getattr(baseline, "net_revenue_yuan", 0.0) or 0.0)

    # 同口径对比：基准策略不参与需求响应，
    # 直接拿"含DR补贴的净收益"去比"不含DR的基准"会虚增收益（稻草人基准的同类错误）。
    # 因此提升幅度一律按"套利收益 - 衰减成本"计算，DR 补贴单独列出。
    net_without_dr = float(report.arbitrage_revenue_yuan) - float(report.degradation_cost_yuan)
    b_arb = getattr(baseline, "arbitrage_revenue_yuan", None)
    b_deg = getattr(baseline, "degradation_cost_yuan", None)
    if baseline is not None and b_arb is not None and b_deg is not None:
        baseline_without_dr = float(b_arb) - float(b_deg)
    else:
        # 基准对象没给拆分项时，退回用它的净收益（此时视为本身就不含DR）
        baseline_without_dr = baseline_net

    improvement = 0.0
    if baseline_without_dr > 1e-6:
        improvement = (net_without_dr - baseline_without_dr) / baseline_without_dr * 100.0

    return DecisionDigest(
        date=str(getattr(report, "date", "")),
        arbitrage_revenue_yuan=float(report.arbitrage_revenue_yuan),
        dr_subsidy_yuan=float(report.dr_subsidy_yuan),
        degradation_cost_yuan=float(report.degradation_cost_yuan),
        net_revenue_yuan=float(report.net_revenue_yuan),
        charge_energy_kwh=float(schedule.charge_energy_kwh),
        discharge_energy_kwh=float(schedule.discharge_energy_kwh),
        max_temp_c=float(temps.max()) if len(temps) else 0.0,
        avg_temp_c=float(temps.mean()) if len(temps) else 0.0,
        temp_headroom_c=float(temp_safe_max - temps.max()) if len(temps) else 0.0,
        equivalent_cycles=float(schedule.equivalent_cycles),
        terminal_soc=float(getattr(schedule, "terminal_soc", 0.0)),
        energy_balance_error_kwh=float(getattr(schedule, "energy_balance_error_kwh", 0.0)),
        soc_violation_steps=int(getattr(schedule, "soc_violation_steps", 0)),
        charge_windows=chg,
        discharge_windows=dis,
        dr_events=dr_lines,
        baseline_net_yuan=baseline_net,
        baseline_net_without_dr_yuan=baseline_without_dr,
        net_without_dr_yuan=net_without_dr,
        improvement_pct=improvement,
        forecast_mape=float(getattr(report, "forecast_mape", 0.0) or 0.0),
        annualized_yuan=float(report.net_revenue_yuan) * 365.0,
        solver_status=str(getattr(schedule, "solver_status", "")),
        alerts=list(getattr(report, "alerts", []) or []),
    )


# ========== 防幻觉校验 ==========
@dataclass
class GroundingReport:
    checked: int = 0
    grounded: int = 0
    unknown: List[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return 1.0 if self.checked == 0 else self.grounded / self.checked


_UNIT_NUM_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(元/kWh|元|kWh|kW|℃|%|次)")


def check_grounding(text: str, digest: DecisionDigest, rtol: float = 0.01, atol: float = 0.05) -> GroundingReport:
    """
    把回答中"带单位"的数字抽出来，逐条回查事实摘要。

    只校验带单位的数字：像"3 条原因"这种计数词不参与，避免无意义误报。
    容差 = |v| * rtol + atol，容忍 LLM 的正常取整（如 1245.30 → 1245）。
    """
    truth = digest.ground_truth_numbers()
    rep = GroundingReport()
    for m in _UNIT_NUM_RE.finditer(text or ""):
        raw = m.group(0)
        val = float(m.group(1))
        rep.checked += 1
        hit = any(abs(val - t) <= (abs(t) * rtol + atol) for t in truth)
        if hit:
            rep.grounded += 1
        else:
            rep.unknown.append(raw)
    return rep


# ========== 提示词 ==========
SYSTEM_PROMPT = """你是工商业储能调度系统的**决策解释助手**。

你会收到一份《事实摘要》，里面所有数字都来自真实的 MILP 求解与后验物理仿真结果。

硬性规则（违反即为错误输出）：
1. 只能使用摘要中出现的数字。需要比较时做定性表述（"高于""接近上限""约为谷段的2倍"），不要自行计算新数字。
2. 你没有计算能力，禁止说"我算了一下""经我测算"。
3. 摘要里没有的数据，直接回答"摘要中没有这项数据"，不要推测。
4. 用中文，结构化输出，关键结论先用一句话说清。

输出结构（默认问题）：
- 一句话结论：今天这套调度方案值不值、值多少、有没有风险
- 关键决策：挑 2-4 条最重要的充/放电决策，说明为什么这样安排（结合电价与SOC/温度状态）
- 约束与风险：温度余量、衰减代价、DR 取舍、需要人工关注的地方
- 可信度说明：求解状态、能量守恒残差、SOC越界步数给出的可信程度判断
"""

DEFAULT_QUESTION = "请解释今日调度方案：为什么这样安排，风险和注意事项是什么。"


# ========== 规则模板（降级路径，也是测试的黄金对照） ==========
def template_explain(digest: DecisionDigest) -> str:
    """不依赖 LLM 的确定性解释，保证无 Key / 断网也能出报告"""
    lines = [
        f"【规则模板解释 · {digest.date}】",
        "",
        f"一句话结论：日净收益 {digest.net_revenue_yuan:.2f} 元"
        + (f"（含DR补贴 {digest.dr_subsidy_yuan:.2f} 元）；同口径（不含DR）"
           f"相对基准策略 {digest.baseline_net_without_dr_yuan:.2f} 元提升 {digest.improvement_pct:.1f}%。"
           if digest.baseline_net_without_dr_yuan > 0 else "。"),
        "",
        "关键决策：",
    ]
    if digest.charge_windows:
        lines.append(f"1. 充电集中在低价时段：{digest.charge_windows[0]}")
    if digest.discharge_windows:
        lines.append(f"2. 放电集中在高价时段：{digest.discharge_windows[0]}")
    if len(digest.discharge_windows) > 1:
        lines.append(f"3. 次优放电窗口：{digest.discharge_windows[1]}")
    lines += [
        "",
        "约束与风险：",
        f"- 最高温度 {digest.max_temp_c:.1f}℃，距 55.0℃ 安全上限余量 {digest.temp_headroom_c:.1f}℃；"
        + ("已触发降额，需关注散热。" if digest.max_temp_c > 45 else "全天处于安全工作区。"),
        f"- 衰减成本 {digest.degradation_cost_yuan:.2f} 元，占套利收益 "
        f"{(digest.degradation_cost_yuan / digest.arbitrage_revenue_yuan * 100 if digest.arbitrage_revenue_yuan else 0):.1f}%；"
        f"等效循环 {digest.equivalent_cycles:.4f} 次。",
        f"- 终值SOC {digest.terminal_soc * 100:.1f}%，能量守恒残差 {digest.energy_balance_error_kwh:.4f} kWh，"
        f"SOC越界 {digest.soc_violation_steps} 步。",
    ]
    if digest.dr_events:
        lines.append(f"- 需求响应：{digest.dr_events[0]}")
    if digest.alerts:
        lines.append(f"- 告警：{digest.alerts[0]}")
    return "\n".join(lines)


# ========== 解释器 ==========
@dataclass
class ExplanationResult:
    text: str
    source: str                      # "llm" | "rule"
    question: str = ""
    grounding: Optional[GroundingReport] = None
    error: str = ""

    @property
    def used_llm(self) -> bool:
        return self.source == "llm"


class LLMExplainer:
    """
    LLM 解释层。

    用法：
        expl = LLMExplainer()                     # 自动从环境变量取 Key（DeepSeek 优先）
        res  = expl.explain(digest)               # 无 Key 自动降级规则模板
        res2 = expl.explain(digest, "为什么14点不放电？")
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.2,
        client: Optional[Callable[..., str]] = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ):
        self.api_key, self.base_url, self.model = resolve_llm_credentials(api_key, base_url, model)
        self.temperature = temperature
        self.timeout_s = timeout_s
        # client 可注入（测试用），默认走 urllib
        self._client = client or call_chat_completions

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def explain(self, digest: DecisionDigest, question: Optional[str] = None) -> ExplanationResult:
        """生成解释；任何失败都降级到规则模板，绝不把异常抛给调用方"""
        q = (question or DEFAULT_QUESTION).strip()
        if not self.enabled:
            return ExplanationResult(text=template_explain(digest), source="rule", question=q)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{digest.to_text()}\n\n---\n\n问题：{q}"},
        ]
        with trace.span("llm_explain", model=self.model) as sp:
            try:
                answer = self._client(
                    messages=messages,
                    api_key=self.api_key,
                    base_url=self.base_url,
                    model=self.model,
                    temperature=self.temperature,
                    timeout_s=self.timeout_s,
                )
            except Exception as e:
                # 降级要留痕：界面上看到"规则模板"却不知道为什么不走 LLM，
                # 是这类系统最难查的一类问题。span 状态记 degraded，主流程照旧不抛。
                if sp is not None:
                    sp.status = "degraded"
                    sp.attrs["error_type"] = type(e).__name__
                return ExplanationResult(
                    text=template_explain(digest), source="rule", question=q,
                    error=f"{type(e).__name__}: {str(e)[:200]}",
                )
            grounding = check_grounding(answer, digest)
            if sp is not None:
                sp.attrs.update(source="llm", answer_len=len(answer or ""),
                                grounding_checked=grounding.checked,
                                grounding_grounded=grounding.grounded,
                                grounding_flagged=len(grounding.unknown))
        if grounding.unknown:
            answer += "\n\n> ⚠️ 以下数字未能在事实摘要中匹配到，请人工核对：" + "、".join(grounding.unknown[:8])
        return ExplanationResult(text=answer, source="llm", question=q, grounding=grounding)
