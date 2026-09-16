"""
储能调度对话Agent

支持自然语言交互：
- 查询："今天收益多少"、"电池温度怎么样"
- 操作："把SOC上限调到80%重跑"、"关闭需求响应再算一次"
- 解释："为什么下午3点放电"、"为什么DR事件被拒绝"
- 对比："和基准策略比怎么样"

双模式：
1. LLM模式：配置API Key后用LangChain Agent + 工具调用
2. 规则模式：无API Key时用关键词匹配，保证可演示
"""
import re
import numpy as np
import pandas as pd
from types import SimpleNamespace
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from src.utils.config import CONFIG, active_config, use_config
from src.utils.logger import get_logger
from src.agents.coordinator_agent import CoordinatorAgent, DailyReport
from src.agents.storage_optimization_agent import StorageOptimizationAgent, ScheduleResult
from src.agents.demand_response_agent import DRSignal

log = get_logger("chat_agent")

# v1.1：LangGraph 是可选依赖。规则模式 / LLM 模式都不需要它，
# 原来这里是硬导入，没装 langgraph 时连对话Agent都用不了。
try:
    from src.agents.langgraph_coordinator import LangGraphCoordinator
except ImportError:  # pragma: no cover
    LangGraphCoordinator = None


# ========== 工具上下文（持有当前调度状态） ==========
@dataclass
class AgentContext:
    """Agent运行上下文，持有当前调度结果供工具调用"""
    coordinator: Optional[object] = None
    report: Optional[DailyReport] = None
    baseline: Optional[ScheduleResult] = None
    viz_data: Optional[Dict] = None
    dr_signals: List[DRSignal] = None
    historical_data: Optional[pd.DataFrame] = None
    selected_date: str = ""
    engine: str = "LangGraph"
    # v1.2：解释层用
    schedule: Optional[ScheduleResult] = None   # 最终调度结果（有则优先，无则从viz_data重建）
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""

    def __post_init__(self):
        if self.dr_signals is None:
            self.dr_signals = []


# ========== 工具函数（规则模式和LLM模式共用） ==========
class SchedulingTools:
    """调度系统工具集，供Agent调用"""

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx
        self._explainer_cache = None
        # 最近一次整日解释的来源："llm" | "rule" | "none"。
        # explain_day() 只返回正文文本，来源信息会在此处留痕，供 API 层透传
        # （否则调用方只能靠解析文本前缀判断走没走 LLM）。
        self.last_explain_source: str = "none"

    # ---------- v1.2：LLM 决策解释层 ----------
    def _explainer(self):
        """惰性创建解释器（无 API Key 时它会自动降级到规则模板）"""
        if self._explainer_cache is None:
            from src.agents.llm_explainer import LLMExplainer
            self._explainer_cache = LLMExplainer(
                api_key=self.ctx.llm_api_key or None,
                base_url=self.ctx.llm_base_url or None,
                model=self.ctx.llm_model or None,
            )
        return self._explainer_cache

    def _digest(self):
        """把当前调度结果压成事实摘要；没有调度结果返回 None"""
        if self.ctx.report is None or not self.ctx.viz_data:
            return None
        from src.agents.llm_explainer import build_digest

        sched = self.ctx.schedule
        if sched is None:
            # 没有显式传 ScheduleResult 时，从可视化数据重建一个够用的壳
            net = np.asarray(self.ctx.viz_data["net_power"], dtype=float)
            soc = np.asarray(self.ctx.viz_data["soc"], dtype=float)
            temp = np.asarray(self.ctx.viz_data["battery_temp"], dtype=float)
            dt = active_config().battery.time_step_hours
            sched = SimpleNamespace(
                net_power_kw=net,
                battery_temp_c=temp,
                charge_energy_kwh=float(np.sum(np.clip(-net, 0, None)) * dt),
                discharge_energy_kwh=float(np.sum(np.clip(net, 0, None)) * dt),
                equivalent_cycles=self.ctx.report.equivalent_cycles,
                terminal_soc=float(soc[-1]) if len(soc) else 0.0,
                energy_balance_error_kwh=0.0,
                soc_violation_steps=0,
                solver_status="reconstructed_from_viz",
            )
        return build_digest(
            report=self.ctx.report,
            schedule=sched,
            price_profile=self.ctx.viz_data.get("price"),
            baseline=self.ctx.baseline,
            dr_responses=self.ctx.viz_data.get("dr_responses"),
            ambient_temp=self.ctx.viz_data.get("ambient_temp"),
        )

    def explain_day(self, question: str = "") -> str:
        """生成今日调度决策的完整解释（LLM 优先，无 Key 降级规则模板）

        副作用：把本次解释的来源写入 ``self.last_explain_source``，
        取值 "llm" | "rule"；无调度结果时为 "none"。
        """
        digest = self._digest()
        if digest is None:
            self.last_explain_source = "none"
            return "请先运行调度。"
        res = self._explainer().explain(digest, question or None)
        self.last_explain_source = res.source
        tag = "🤖 LLM 决策解释" if res.used_llm else "📋 规则模板解释（未配置 API Key）"
        head = f"{tag}\n\n"
        if res.error:
            head += f"> 已降级：{res.error}\n\n"
        return head + res.text

    def ask(self, question: str) -> str:
        """基于当日调度事实摘要回答自然语言问题（数字全部来自摘要，不现算）"""
        digest = self._digest()
        if digest is None:
            return "请先运行调度。"
        res = self._explainer().explain(digest, question)
        if res.used_llm:
            return res.text
        # 规则模式下没有 LLM，退回关键词路由
        return RuleBasedAgent(self).respond(question)

    def get_report(self) -> str:
        """获取当日能效报表摘要"""
        if self.ctx.report is None:
            return "尚未运行调度，请先点击「运行调度」按钮。"
        r = self.ctx.report
        return (
            f"📊 {r.date} 能效报表：\n"
            f"• 峰谷套利收益：{r.arbitrage_revenue_yuan:.2f} 元\n"
            f"• 需求响应补贴：{r.dr_subsidy_yuan:.2f} 元\n"
            f"• 电池衰减成本：{r.degradation_cost_yuan:.2f} 元\n"
            f"• 日净收益：{r.net_revenue_yuan:.2f} 元\n"
            f"• 充电量：{r.charge_energy_kwh:.1f} kWh，放电量：{r.discharge_energy_kwh:.1f} kWh\n"
            f"• 最高电池温度：{r.max_battery_temp_c:.1f}℃\n"
            f"• 等效循环：{r.equivalent_cycles:.4f} 次\n"
            f"• 负荷预测MAPE：{r.forecast_mape}%\n"
            f"• DR响应：{r.dr_events_count} 个事件"
        )

    def compare_baseline(self) -> str:
        """对比优化策略与基准策略"""
        if self.ctx.report is None or self.ctx.baseline is None:
            return "请先运行调度。"
        r = self.ctx.report
        b = self.ctx.baseline
        rev_imp = (r.arbitrage_revenue_yuan - b.arbitrage_revenue_yuan) / b.arbitrage_revenue_yuan * 100
        deg_red = (b.degradation_cost_yuan - r.degradation_cost_yuan) / b.degradation_cost_yuan * 100
        return (
            f"📈 优化策略 vs 基准策略（低谷充满高峰放完）：\n"
            f"• 套利收益：{b.arbitrage_revenue_yuan:.0f} → {r.arbitrage_revenue_yuan:.0f} 元（{rev_imp:+.1f}%）\n"
            f"• 衰减成本：{b.degradation_cost_yuan:.0f} → {r.degradation_cost_yuan:.0f} 元（降低{deg_red:.1f}%）\n"
            f"• 最高温度：{b.max_battery_temp_c:.1f}℃ → {r.max_battery_temp_c:.1f}℃\n"
            f"  （基准策略不考虑热约束，温度飙到{b.max_battery_temp_c:.0f}℃，工程上不可行）\n"
            f"• 等效循环：{b.equivalent_cycles:.4f} → {r.equivalent_cycles:.4f} 次"
        )

    def explain_schedule(self, hour: int) -> str:
        """解释某时段的调度策略"""
        if self.ctx.viz_data is None:
            return "请先运行调度。"
        idx = hour * 4  # 15分钟粒度
        if idx >= 96:
            return "请输入0-23之间的小时。"
        net_power = self.ctx.viz_data["net_power"][idx]
        price = self.ctx.viz_data["price"][idx]
        soc = self.ctx.viz_data["soc"][idx] * 100
        temp = self.ctx.viz_data["battery_temp"][idx]

        if net_power > 50:
            action = f"放电 {net_power:.0f}kW"
            reason = f"此时电价{price:.2f}元/kWh处于高峰，放电套利；SOC={soc:.0f}%，温度={temp:.1f}℃安全"
        elif net_power < -50:
            action = f"充电 {abs(net_power):.0f}kW"
            reason = f"此时电价{price:.2f}元/kWh处于低谷，充电储能；SOC={soc:.0f}%，温度={temp:.1f}℃"
        else:
            action = "待机（功率接近0）"
            reason = f"电价{price:.2f}元/kWh处于平段，充放套利空间小，保持待机减少衰减"

        return f"⏰ {hour}:00 调度策略：{action}\n💡 原因：{reason}"

    def get_thermal_info(self) -> str:
        """获取电池热状态"""
        if self.ctx.viz_data is None:
            return "请先运行调度。"
        temps = self.ctx.viz_data["battery_temp"]
        max_temp = np.max(temps)
        avg_temp = np.mean(temps)
        over_temp_count = np.sum(temps >= 45)
        max_idx = np.argmax(temps)
        max_hour = max_idx // 4

        status = "✅ 全天正常" if max_temp < 45 else "⚠️ 触发降额" if max_temp < 55 else "🛑 超温"
        return (
            f"🌡️ 电池热状态（一阶RC模型）：\n"
            f"• 最高温度：{max_temp:.1f}℃（{max_hour}:00左右）\n"
            f"• 平均温度：{avg_temp:.1f}℃\n"
            f"• 超45℃时段：{over_temp_count}个（共96点）\n"
            f"• 状态：{status}\n"
            f"• 热容C={active_config().battery.thermal_capacity_kj_k}kJ/K，热阻Rth={active_config().battery.thermal_resistance_k_w}K/W"
        )

    def get_dr_info(self) -> str:
        """获取需求响应事件详情"""
        if not self.ctx.viz_data or not self.ctx.viz_data.get("dr_responses"):
            return "当前无需求响应事件。"
        lines = ["📡 需求响应事件："]
        for i, dr in enumerate(self.ctx.viz_data["dr_responses"]):
            sig = self.ctx.dr_signals[i] if i < len(self.ctx.dr_signals) else None
            sig_str = f"{pd.to_datetime(sig.start_time).strftime('%H:%M')}-{pd.to_datetime(sig.end_time).strftime('%H:%M')}" if sig else "?"
            if dr.accepted:
                lines.append(
                    f"  ✅ 事件{i+1}（{sig_str}）：接受响应\n"
                    f"     目标{sig.target_reduction_kw:.0f}kW → 实际{dr.response_power_kw:.0f}kW\n"
                    f"     补贴{dr.subsidy_revenue_yuan:.1f}元，衰减成本{dr.degradation_cost_yuan:.1f}元，净收益{dr.net_dr_revenue_yuan:.1f}元"
                )
            else:
                lines.append(f"  ❌ 事件{i+1}（{sig_str}）：拒绝 - {dr.reason}")
        return "\n".join(lines)

    def run_with_params(
        self,
        soc_min: Optional[int] = None,
        soc_max: Optional[int] = None,
        rated_power: Optional[int] = None,
        include_thermal: Optional[bool] = None,
        include_degradation: Optional[bool] = None,
        enable_dr: Optional[bool] = None,
    ) -> str:
        """用新参数重新运行调度"""
        if self.ctx.historical_data is None:
            return "缺少历史数据。"

        # P0-04：参数打包为配置快照注入，不再改动全局 CONFIG
        from dataclasses import replace as _dc_replace
        _bat = active_config().battery
        if soc_min is not None:
            _bat = _dc_replace(_bat, soc_min=soc_min / 100)
        if soc_max is not None:
            _bat = _dc_replace(_bat, soc_max=soc_max / 100)
        if rated_power is not None:
            _bat = _dc_replace(_bat, rated_power_kw=rated_power)
        _cfg = active_config() if _bat is active_config().battery else _dc_replace(active_config(), battery=_bat)

        dr_signals = self.ctx.dr_signals if enable_dr is not False else []

        # 运行
        with use_config(_cfg):
            if self.ctx.engine == "LangGraph" and LangGraphCoordinator is not None:
                coordinator = LangGraphCoordinator(_cfg)
            else:
                coordinator = CoordinatorAgent(config=_cfg)
            report = coordinator.run_daily_scheduling(
                date=self.ctx.selected_date,
                historical_data=self.ctx.historical_data,
                dr_signals=dr_signals,
                use_ml_forecast=True,
                include_thermal=include_thermal if include_thermal is not None else True,
                include_degradation=include_degradation if include_degradation is not None else True,
            )

        # 基准
        day_data = self.ctx.historical_data[
            self.ctx.historical_data["timestamp"].dt.date == pd.to_datetime(self.ctx.selected_date).date()
        ]
        baseline = StorageOptimizationAgent(_cfg).baseline_strategy(
            day_data["price_yuan_per_kwh"].values,
            day_data["ambient_temp_c"].values,
        )

        # 更新上下文
        self.ctx.coordinator = coordinator
        self.ctx.report = report
        self.ctx.baseline = baseline
        self.ctx.viz_data = coordinator.get_visualization_data()

        param_changes = []
        if soc_min is not None: param_changes.append(f"SOC下限={soc_min}%")
        if soc_max is not None: param_changes.append(f"SOC上限={soc_max}%")
        if rated_power is not None: param_changes.append(f"额定功率={rated_power}kW")
        if include_thermal is not None: param_changes.append(f"热约束={'开' if include_thermal else '关'}")
        if enable_dr is not None: param_changes.append(f"DR={'开' if enable_dr else '关'}")

        return (
            f"🔄 已用新参数重新运行：{', '.join(param_changes) if param_changes else '默认参数'}\n"
            f"新结果：净收益 {report.net_revenue_yuan:.0f} 元，"
            f"套利 {report.arbitrage_revenue_yuan:.0f} 元，"
            f"最高温度 {report.max_battery_temp_c:.1f}℃"
        )


# ========== 规则模式Agent（无API Key时使用） ==========
class RuleBasedAgent:
    """基于关键词匹配的对话Agent，保证无API Key时也能演示"""

    # 供 API 层透传的模式标识（"rule" | "llm"），避免调用方用 isinstance 猜
    mode = "rule"

    def __init__(self, tools: SchedulingTools):
        self.tools = tools

    def respond(self, user_input: str) -> str:
        text = user_input.lower()

        # 对比基准
        if any(k in text for k in ["对比", "基准", "比较", "比怎么样", "相比", "差距"]):
            return self.tools.compare_baseline()

        # 报表/收益查询
        if any(k in text for k in ["收益", "赚", "报表", "结果", "多少钱", "净收益", "套利", "营收"]):
            return self.tools.get_report()

        # 温度/热模型
        if any(k in text for k in ["温度", "热", "温升", "过温", "安全"]):
            return self.tools.get_thermal_info()

        # 需求响应
        if any(k in text for k in ["dr", "需求响应", "削峰", "补贴", "响应"]):
            return self.tools.get_dr_info()

        # 解释某时段
        hour_match = re.search(r'(\d{1,2})\s*[点时:：]', user_input)
        if hour_match and any(k in text for k in ["为什么", "解释", "为啥", "怎么", "调度", "充", "放"]):
            hour = int(hour_match.group(1))
            return self.tools.explain_schedule(hour)

        # v1.2：整日决策解释（LLM 优先，无 Key 自动降级规则模板）
        if any(k in text for k in ["解释", "为什么", "为啥", "分析", "总结", "解读", "决策", "怎么安排的", "思路"]):
            return self.tools.explain_day(user_input)

        # 重新运行/调参
        if any(k in text for k in ["重跑", "重新", "再跑", "调", "改成", "设为", "调到"]):
            params = {}
            soc_min_m = re.search(r'soc.{0,5}(?:下限|最低|min).{0,5}(\d{1,3})', text)
            soc_max_m = re.search(r'soc.{0,5}(?:上限|最高|max).{0,5}(\d{1,3})', text)
            power_m = re.search(r'(\d{3,4})\s*(?:kw|千瓦|功率)', text)
            if soc_min_m: params["soc_min"] = int(soc_min_m.group(1))
            if soc_max_m: params["soc_max"] = int(soc_max_m.group(1))
            if power_m: params["rated_power"] = int(power_m.group(1))
            if "热约束" in text or "温度约束" in text:
                params["include_thermal"] = "关" not in text and "去掉" not in text
            if "需求响应" in text or "dr" in text:
                params["enable_dr"] = "关" not in text and "去掉" not in text
            if params:
                return self.tools.run_with_params(**params)
            return "我可以帮你调整参数重跑，例如：'把SOC上限调到80%重跑'、'关掉热约束再算一次'"

        # 帮助
        if any(k in text for k in ["帮助", "能做什么", "功能", "你会", "help"]):
            return (
                "🤖 我是储能调度助手，可以帮你：\n"
                "📊 查询：'今天收益多少'、'温度怎么样'、'DR事件详情'\n"
                "🔍 解释：'为什么15点放电'、'解释一下调度策略'\n"
                "🔄 操作：'把SOC上限调到80%重跑'、'关掉DR再算一次'\n"
                "📈 对比：'和基准比怎么样'、'优化效果如何'"
            )

        # 默认
        return (
            f"你说的是「{user_input}」，我可以理解这些指令：\n"
            "• 问收益/温度/DR事件\n"
            "• 问某时段为什么充/放电（如'为什么15点放电'）\n"
            "• 让我调参重跑（如'SOC上限改80%重跑'）\n"
            "• 问和基准策略对比\n"
            "输入'帮助'查看全部功能。"
        )


# ========== LLM模式Agent（有API Key时使用） ==========
def chunk_text(chunk) -> str:
    """从 LangChain 消息块中提取纯文本增量。

    兼容两种 content 形态：纯字符串，或 content-blocks 列表
    （部分 OpenAI 兼容网关返回 [{"type":"text","text":"..."}]）。
    工具调用阶段的 chunk 无文本，返回空串由调用方跳过。
    """
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict) and isinstance(b.get("text"), str):
                parts.append(b["text"])
        return "".join(parts)
    return ""


class LLMAgent:
    """基于LangChain的LLM Agent，支持工具调用"""

    # 供 API 层透传的模式标识（"rule" | "llm"）
    mode = "llm"

    def __init__(self, tools: SchedulingTools, api_key: str, base_url: str = None, model: str = "gpt-4o-mini"):
        from langchain_openai import ChatOpenAI
        from langchain_core.tools import tool
        # 🔴fix22：langchain 1.x 移除了 create_tool_calling_agent/AgentExecutor，
        # 迁移到 v1 新 API create_agent（LangGraph 内核）。
        from langchain.agents import create_agent

        self.tools_obj = tools

        # 定义工具
        @tool
        def get_report() -> str:
            """获取当日能效报表，包含收益、温度、SOC等关键指标"""
            return tools.get_report()

        @tool
        def compare_baseline() -> str:
            """对比优化策略与基准策略的差异"""
            return tools.compare_baseline()

        @tool
        def explain_schedule(hour: int) -> str:
            """解释指定小时(0-23)的充放电调度策略和原因"""
            return tools.explain_schedule(hour)

        @tool
        def get_thermal_info() -> str:
            """获取电池温度状态和热模型参数"""
            return tools.get_thermal_info()

        @tool
        def get_dr_info() -> str:
            """获取需求响应事件的详细处理结果"""
            return tools.get_dr_info()

        @tool
        def run_with_params(
            soc_min: int = None,
            soc_max: int = None,
            rated_power: int = None,
            include_thermal: bool = None,
            enable_dr: bool = None,
        ) -> str:
            """用新参数重新运行调度。参数可选，不传则保持原值。
            soc_min/soc_max: 百分比整数(如20,90)
            rated_power: 额定功率kW
            include_thermal: 是否包含热安全约束
            enable_dr: 是否启用需求响应
            """
            return tools.run_with_params(
                soc_min=soc_min, soc_max=soc_max, rated_power=rated_power,
                include_thermal=include_thermal, enable_dr=enable_dr,
            )

        @tool
        def explain_day() -> str:
            """生成今日储能调度方案的完整决策解释：为什么这么安排、风险与注意事项"""
            return tools.explain_day()

        @tool
        def ask(question: str) -> str:
            """基于当日调度事实摘要回答自然语言问题，例如'为什么14点不放电'"""
            return tools.ask(question)

        tool_list = [get_report, compare_baseline, explain_schedule,
                     get_thermal_info, get_dr_info, run_with_params,
                     explain_day, ask]

        # LLM（超时/重试：LLM 卡死不能拖死对话主流程）
        llm_kwargs = {"api_key": api_key, "model": model, "temperature": 0,
                      "timeout": 60, "max_retries": 1}
        if base_url:
            llm_kwargs["base_url"] = base_url
        llm = ChatOpenAI(**llm_kwargs)

        system_prompt = """你是一个工商业储能调度专家助手，基于一阶RC热模型和MILP优化的多智能体系统。

你的职责：
1. 用通俗的语言解释储能调度结果
2. 主动调用工具获取准确数据，不要编造数字
3. 解释时突出能动专业特色：热管理、寿命衰减、传热学
4. 回答简洁，关键数据用列表展示

可用工具：查询报表、对比基准、解释某时段调度、查询温度、查询DR事件、调参重跑。

【输出排版规范】（界面按 Markdown 渲染，请严格遵守，保证回答清晰易读）
结构：
- 先一句话给结论，再分点展开；常规回答控制在 400 字内，用户明确要求"详细"时才展开
- 层级顺序固定为：结论 → 关键数据 → 依据说明 → 风险/建议（缺项则跳过，不要凑标题）

标题：
- 用 `##` 起步，最多到 `####`，不要跳级，也不要使用 `#`（一级标题属于页面）
- 标题写短名词短语（≤12 字），不要写成整句；单次回答标题不超过 4 个

列表：
- 并列关系用 `-`，前后有先后顺序用 `1.`
- 每条不超过两行；嵌套最多一层（续行缩进 2 个空格）
- 列表项以「**关键词**：说明」开头，便于扫读

强调与表格：
- 用 `**加粗**` 只标关键数字和核心结论，每节不超过 2 处，禁止整段加粗或给每个名词加粗
- 仅在对比 3 个以上对象的同类属性时用表格；列数不超过 4 列，单元格内不换行
- 表格必须有分隔行 `|---|---|`，否则无法渲染

代码与公式：
- 参数、变量、表达式用行内 `代码` 包裹，例如 `C·dT/dt = Q_gen − (T−T_amb)/Rth`
- 多行示例或数据片段用三反引号围栏代码块；短表达式不要用代码块

引用与分隔：
- `>` 引用只用于标注已有系统结论或风险提示，不要用它代替正文
- 段落之间空一行即可，不要连续空行堆砌间距
- 短回答不要用 `---` 分隔线切割

禁止：
- 不要输出任何 HTML 标签（如 `<br>`、`<div>`）
- 不要在标题里堆 emoji，正文 emoji 总数不超过 2 个
- 不要用「一、二、三」+ 粗体模拟标题，标题一律用 `##` 语法"""

        self.executor = create_agent(llm, tools=tool_list, system_prompt=system_prompt)

    def respond(self, user_input: str) -> str:
        try:
            result = self.executor.invoke(
                {"messages": [{"role": "user", "content": user_input}]})
            return result["messages"][-1].content
        except Exception as e:
            return f"LLM调用出错：{str(e)[:200]}\n你可以检查API Key是否正确，或切换到规则模式。"

    async def astream(self, user_input: str):
        """流式对话：逐块产出模型文本增量（供 SSE 端点消费）。

        create_agent 返回的是 LangGraph 内核的 CompiledStateGraph，
        astream(stream_mode="messages") 按 token 产出 (AIMessageChunk, metadata)；
        工具调用/工具结果阶段 content 为空，由 chunk_text 过滤为空串后跳过。
        """
        async for chunk, _meta in self.executor.astream(
            {"messages": [{"role": "user", "content": user_input}]},
            stream_mode="messages",
        ):
            text = chunk_text(chunk)
            if text:
                yield text


# ========== Agent工厂 ==========
def create_agent(
    ctx: AgentContext,
    api_key: str = "",
    base_url: str = "",
    model: str = "gpt-4o-mini",
) -> object:
    """创建Agent，有API Key用LLM模式，否则用规则模式"""
    # v1.2：把 LLM 配置同步到上下文，供解释层使用（工具解释与整日解释共用同一套凭据）
    ctx.llm_api_key = api_key or ""
    ctx.llm_base_url = base_url or ""
    ctx.llm_model = model or ""
    tools = SchedulingTools(ctx)
    if api_key:
        try:
            return LLMAgent(tools, api_key, base_url or None, model)
        except ImportError as e:
            # 🔴fix22 修复：此前 except Exception 静默回退规则模式——langchain-openai 未装时
            # 用户配好 Key 也永远出不了 LLM 回复，且无任何提示。现在至少留下可查的日志。
            log.warning("langchain-openai 未安装或导入失败（%s），对话回退规则模式。"
                        "修复：pip install langchain-openai", e)
            return RuleBasedAgent(tools)
        except Exception as e:
            log.warning("LLMAgent 初始化失败（%s），对话回退规则模式", e)
            return RuleBasedAgent(tools)
    return RuleBasedAgent(tools)
