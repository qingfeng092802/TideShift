"""
LangGraph 版本调度协调器

用 LangGraph 有向图替换纯 Python 线性编排：
- 节点：init → load_forecast → storage_optimization → dr_handler（循环）→ finalize
- 条件边：DR事件逐个循环处理，全部处理完后进入汇总
- 状态：SchedulingState 在节点间传递，支持中断、回放、可视化图结构

与原 CoordinatorAgent 接口完全兼容，可直接替换使用。
"""
from src.utils.logger import get_logger
log = get_logger(__name__)
import operator
import numpy as np
import pandas as pd
from typing import TypedDict, Annotated, Optional, List, Dict
from langgraph.graph import StateGraph, END

from src.utils.config import CONFIG, active_config
from src.agents.load_forecast_agent import LoadForecastAgent, ForecastResult
from src.agents.storage_optimization_agent import StorageOptimizationAgent, ScheduleResult
from src.agents.demand_response_agent import DemandResponseAgent, DRSignal, DRResponse
from src.agents.coordinator_agent import DailyReport


# ========== 图状态定义 ==========
class SchedulingState(TypedDict, total=False):
    """
    LangGraph 调度状态

    所有字段都是可选的（total=False），节点只返回需要更新的字段。
    dr_responses 用 operator.add 实现追加语义。
    """
    # 输入配置
    date: str
    historical_data: pd.DataFrame
    dr_signals: List[DRSignal]
    use_ml_forecast: bool
    include_thermal: bool
    include_degradation: bool

    # 当日数据
    price_profile: np.ndarray
    ambient_temp_profile: np.ndarray
    load_profile: np.ndarray
    time_index: pd.DatetimeIndex

    # 各Agent输出
    forecast_result: ForecastResult
    feature_importance: Dict
    base_schedule: ScheduleResult
    dr_responses: Annotated[List[DRResponse], operator.add]  # 追加语义
    final_power: np.ndarray
    final_schedule: ScheduleResult

    # DR循环控制
    dr_index: int              # 当前处理到第几个DR事件
    total_dr_subsidy: float
    dr_count: int

    # 系统状态
    current_soc: float
    current_battery_temp: float
    alerts: Annotated[List[str], operator.add]  # 追加语义

    # 最终报表
    report: DailyReport

    # LLM 决策解释层
    digest: object          # DecisionDigest（llm_explainer，惰性导入避免硬依赖）
    explanation: str
    explanation_source: str
    baseline_result: object  # 可选的基准 ScheduleResult，用于解释层做对照


# ========== 节点函数 ==========

def init_node(state: SchedulingState) -> Dict:
    """节点1：初始化状态，加载当日电价/温度/时间数据"""
    date = state["date"]
    historical_data = state["historical_data"]
    cfg = CONFIG

    log.info("\n[LangGraph] ▶ init_node：加载当日数据")

    day_data = historical_data[
        historical_data["timestamp"].dt.date == pd.to_datetime(date).date()
    ].reset_index(drop=True)

    if len(day_data) == 0:
        from src.data.data_generator import generate_price_profile
        price_profile = generate_price_profile(pd.date_range(start=date, periods=96, freq="15min"))
        ambient_temp_profile = np.ones(96) * 30.0
        time_index = pd.date_range(start=date, periods=96, freq="15min")
        load_profile = None
    else:
        price_profile = day_data["price_yuan_per_kwh"].values
        ambient_temp_profile = day_data["ambient_temp_c"].values
        load_profile = day_data["load_kw"].values
        time_index = day_data["timestamp"].values

    log.info(f"  日期: {date}, 数据点: {len(price_profile)}")
    log.info(f"  电价范围: {price_profile.min():.3f} - {price_profile.max():.3f} 元/kWh")

    return {
        "price_profile": price_profile,
        "ambient_temp_profile": ambient_temp_profile,
        "load_profile": load_profile,
        "time_index": time_index,
        "dr_index": 0,
        "total_dr_subsidy": 0.0,
        "dr_count": 0,
        "current_soc": 0.5,
        "current_battery_temp": 25.0,
    }


def load_forecast_node(state: SchedulingState) -> Dict:
    """节点2：负荷预测Agent（XGBoost + 传热学物理修正）"""
    log.info("\n[LangGraph] ▶ load_forecast_node：负荷预测Agent")

    xgb_params = state.get("xgb_params", None)
    agent = LoadForecastAgent(active_config(), xgb_params=xgb_params)
    date = state["date"]
    historical_data = state["historical_data"]
    use_ml = state.get("use_ml_forecast", True)

    try:
        if use_ml:
            forecast = agent.predict(historical_data, date, apply_physical_correction=True)
        else:
            forecast = agent.predict_simple(historical_data, date, apply_physical_correction=True)

        log.info(f"  预测完成，MAPE: {forecast.mape}%")
        log.info(f"  物理修正幅度: {forecast.correction_magnitude_kw:.1f} kW")

        alerts = []
        if forecast.mape > 15:
            alerts.append(f"负荷预测偏差过大（MAPE={forecast.mape}%），建议增加实时修正频率")
            log.info(f"  [告警] {alerts[0]}")
        # 降级告警：非空表示本次未走 XGBoost 主路径（历史数据不足等），
        # 必须显式暴露，避免报表上默默展示一组口径不同的数字。
        if getattr(forecast, "fallback_reason", ""):
            alerts.append(f"负荷预测已降级：{forecast.fallback_reason}")
            log.warning(f"  [告警] {alerts[-1]}")

        return {
            "forecast_result": forecast,
            "load_profile": forecast.forecast_load_kw,
            "alerts": alerts,
            "feature_importance": agent.get_feature_importance(),
        }
    except Exception as e:
        log.warning(f"  [警告] 负荷预测失败({e})，使用历史均值替代")
        forecast = agent.predict_simple(historical_data, date)
        return {
            "forecast_result": forecast,
            "load_profile": forecast.forecast_load_kw,
            "feature_importance": {},
            # 降级同样上报：此前异常分支不返回 alerts，报表看不出走了降级路径。
            "alerts": [f"负荷预测异常，已降级为历史均值预测（{type(e).__name__}）：{str(e)[:120]}"],
        }


def storage_optimization_node(state: SchedulingState) -> Dict:
    """节点3：储能优化调度Agent（MILP + 热约束 + 寿命衰减）"""
    log.info("\n[LangGraph] ▶ storage_optimization_node：储能优化调度Agent")

    agent = StorageOptimizationAgent(active_config())
    schedule = agent.optimize(
        price_profile=state["price_profile"],
        load_profile=state["load_profile"],
        ambient_temp_profile=state["ambient_temp_profile"],
        initial_soc=state.get("current_soc", 0.5),
        include_thermal_constraint=state.get("include_thermal", True),
        include_degradation_cost=state.get("include_degradation", True),
    )

    log.info(f"  求解状态: {schedule.solver_status}")
    log.info(f"  基准套利收益: {schedule.arbitrage_revenue_yuan:.2f} 元")
    log.info(f"  基准衰减成本: {schedule.degradation_cost_yuan:.2f} 元")
    log.info(f"  最高温度: {schedule.max_battery_temp_c:.1f} ℃")

    alerts = []
    if schedule.max_battery_temp_c > active_config().battery.temp_normal_max:
        alerts.append(f"电池温度超过正常上限（{schedule.max_battery_temp_c:.1f}℃），已触发降额")
        log.info(f"  [告警] {alerts[0]}")

    return {
        "base_schedule": schedule,
        "final_power": schedule.net_power_kw.copy(),
        "alerts": alerts,
    }


def dr_handler_node(state: SchedulingState) -> Dict:
    """
    节点4：需求响应Agent（循环处理单个DR事件）

    每次调用处理一个DR事件，dr_index + 1。
    通过条件边判断是否继续循环。
    """
    dr_signals = state.get("dr_signals", [])
    dr_index = state.get("dr_index", 0)

    if dr_index >= len(dr_signals):
        return {}

    dr_signal = dr_signals[dr_index]
    # 创建副本，避免DR Agent修改原始对象（热降额时会改target_reduction_kw）
    import copy
    dr_signal = copy.deepcopy(dr_signal)
    log.info(f"\n[LangGraph] ▶ dr_handler_node：处理DR事件 {dr_index+1}/{len(dr_signals)}")
    log.info(f"  时段: {dr_signal.start_time} - {dr_signal.end_time}, "
          f"目标削减{dr_signal.target_reduction_kw:.0f}kW")


    agent = DemandResponseAgent(active_config())  # 与 MILP 同一配置源（原硬编码全局 CONFIG）
    base_schedule = state["base_schedule"]
    final_power = state["final_power"].copy()

    # 确定DR时段开始时的电池温度和SOC
    start_dt = pd.to_datetime(dr_signal.start_time)
    time_idx_pd = pd.to_datetime(state["time_index"])
    dr_start_mask = time_idx_pd >= start_dt
    if dr_start_mask.any():
        dr_start_idx = int(np.argmax(dr_start_mask))
        dr_initial_temp = (base_schedule.battery_temp_c[dr_start_idx]
                          if dr_start_idx < len(base_schedule.battery_temp_c)
                          else state.get("current_battery_temp", 25.0))
        dr_initial_soc = (base_schedule.soc[dr_start_idx]
                         if dr_start_idx < len(base_schedule.soc)
                         else state.get("current_soc", 0.5))
    else:
        dr_initial_temp = state.get("current_battery_temp", 25.0)
        dr_initial_soc = state.get("current_soc", 0.5)

    dr_response = agent.evaluate_and_respond(
        dr_signal=dr_signal,
        base_schedule_power=final_power,
        current_soc=dr_initial_soc,
        current_battery_temp=dr_initial_temp,
        ambient_temp_profile=state["ambient_temp_profile"],
        load_profile=state["load_profile"],
        time_index=pd.to_datetime(state["time_index"]),
    )

    updates = {
        "dr_responses": [dr_response],  # 用 operator.add 追加
        "dr_index": dr_index + 1,
    }

    if dr_response.accepted:
        updates["final_power"] = dr_response.adjusted_power_kw
        updates["total_dr_subsidy"] = state.get("total_dr_subsidy", 0) + dr_response.subsidy_revenue_yuan
        updates["dr_count"] = state.get("dr_count", 0) + 1
        log.info(f"  ✓ 接受响应，补贴{dr_response.subsidy_revenue_yuan:.2f}元，"
              f"实际响应{dr_response.response_power_kw:.0f}kW")

    else:
        log.warning(f"  ✗ 拒绝响应：{dr_response.reason}")

    return updates


def should_continue_dr(state: SchedulingState) -> str:
    """
    条件边：判断是否继续处理DR事件
    返回 "dr_handler" 继续循环，返回 "finalize" 进入汇总
    """
    dr_signals = state.get("dr_signals", [])
    dr_index = state.get("dr_index", 0)
    if dr_index < len(dr_signals):
        return "dr_handler"
    return "finalize"


def finalize_node(state: SchedulingState) -> Dict:
    """节点5：结果汇总（SOC后处理 + 收益计算 + 温度仿真 + 报表）"""
    log.info("\n[LangGraph] ▶ finalize_node：汇总最终结果")

    cfg = active_config().battery
    dt = cfg.time_step_hours
    final_power = state["final_power"].copy()

    # SOC后处理：物理可行性裁剪
    soc = state.get("current_soc", 0.5)
    # 此前 np.clip(soc, soc_min, soc_max) 掩盖越界（soc_violation_steps 永远报 0），
    # 且 ScheduleResult 未传 energy_balance_error_kwh（恒 0.0）→ 自检形同虚设。
    # 现在：裁剪照做（保证物理可行），但统计被钳制的总能量并显式透传。
    soc_violation_steps = 0
    clipped_energy_kwh = 0.0
    for i in range(len(final_power)):
        power = final_power[i]
        if power > 0:  # 放电
            max_discharge = (soc - cfg.soc_min) * cfg.rated_capacity_kwh / dt * cfg.discharge_efficiency
            final_power[i] = min(power, max(0, max_discharge))
            clipped_energy_kwh += abs(power - final_power[i]) * dt  # 被钳掉的放电功率→能量
            soc -= final_power[i] * dt / (cfg.discharge_efficiency * cfg.rated_capacity_kwh)
        elif power < 0:  # 充电
            max_charge = (cfg.soc_max - soc) * cfg.rated_capacity_kwh / dt / cfg.charge_efficiency
            new_power = max(power, -max(0, max_charge))
            clipped_energy_kwh += abs(power - new_power) * dt
            final_power[i] = new_power
            soc += abs(final_power[i]) * dt * cfg.charge_efficiency / cfg.rated_capacity_kwh
        if soc > cfg.soc_max + 1e-6 or soc < cfg.soc_min - 1e-6:
            soc_violation_steps += 1
        soc = np.clip(soc, cfg.soc_min, cfg.soc_max)

    # 计算收益
    charge_power = np.where(final_power < 0, -final_power, 0)
    discharge_power = np.where(final_power > 0, final_power, 0)
    charge_energy = np.sum(charge_power) * dt
    discharge_energy = np.sum(discharge_power) * dt
    arbitrage_rev = float(np.sum(
        (discharge_power * state["price_profile"] -
         charge_power * state["price_profile"]) * dt
    ))

    # 温度仿真
    storage_agent = StorageOptimizationAgent(active_config())
    thermal_result = storage_agent.thermal_model.simulate_full_day(
        final_power, state["ambient_temp_profile"],
        initial_temp=state["ambient_temp_profile"][0],
        initial_soc=state.get("current_soc", 0.5),
    )

    # 衰减计算
    deg_result = storage_agent.degradation_model.compute_degradation_from_power(
        final_power, initial_soc=state.get("current_soc", 0.5)
    )

    total_dr_subsidy = state.get("total_dr_subsidy", 0)
    total_degradation_cost = deg_result.degradation_cost_yuan
    net_revenue = arbitrage_rev + total_dr_subsidy - total_degradation_cost

    # 最终调度结果
    final_schedule = ScheduleResult(
        charge_power_kw=charge_power,
        discharge_power_kw=discharge_power,
        net_power_kw=final_power,
        soc=deg_result.soc_trajectory,
        battery_temp_c=thermal_result["temperature_c"],
        arbitrage_revenue_yuan=round(arbitrage_rev, 2),
        degradation_cost_yuan=round(total_degradation_cost, 2),
        net_revenue_yuan=round(net_revenue, 2),
        charge_energy_kwh=round(charge_energy, 1),
        discharge_energy_kwh=round(discharge_energy, 1),
        max_battery_temp_c=round(thermal_result["max_temperature_c"], 1),
        equivalent_cycles=round(deg_result.equivalent_cycles, 4),
        solver_status="Final_LangGraph_DR_Adjusted",
        terminal_soc=round(float(soc), 4),   # 补上终值SOC，否则解释层会显示成 0.0%
        # 显式传守恒残差与越界步数（此前默认 0.0 / 0，自检失效）。
        # 能量守恒残差 = DR 调整后被 SOC 物理裁剪钳掉的能量（>0 说明 DR 追加响应
        # 一度超出电池物理能力，前端应提示）。
        energy_balance_error_kwh=round(float(clipped_energy_kwh), 4),
        soc_violation_steps=int(soc_violation_steps),
    )

    # 日报表
    forecast_result = state.get("forecast_result")
    report = DailyReport(
        date=state["date"],
        arbitrage_revenue_yuan=arbitrage_rev,
        dr_subsidy_yuan=total_dr_subsidy,
        degradation_cost_yuan=total_degradation_cost,
        net_revenue_yuan=net_revenue,
        charge_energy_kwh=charge_energy,
        discharge_energy_kwh=discharge_energy,
        max_battery_temp_c=thermal_result["max_temperature_c"],
        equivalent_cycles=deg_result.equivalent_cycles,
        forecast_mape=forecast_result.mape if forecast_result else 0,
        dr_events_count=state.get("dr_count", 0),
        alerts=state.get("alerts", []),
    )

    log.info(f"\n{'='*60}")
    log.info(f"[LangGraph] 日调度完成")
    log.info(f"{'='*60}")
    log.info(f"  峰谷套利收益: {report.arbitrage_revenue_yuan:.2f} 元")
    log.info(f"  需求响应补贴: {report.dr_subsidy_yuan:.2f} 元")
    log.info(f"  电池衰减成本: {report.degradation_cost_yuan:.2f} 元")
    log.info(f"  净收益:       {report.net_revenue_yuan:.2f} 元")
    log.info(f"  最高电池温度: {report.max_battery_temp_c:.1f} ℃")
    log.info(f"  等效循环:     {report.equivalent_cycles:.4f} 次")
    log.info(f"  DR响应:       {report.dr_events_count} 个事件")

    return {
        "final_schedule": final_schedule,
        "report": report,
    }


def explanation_node(state: SchedulingState) -> Dict:
    """
    节点6：LLM 决策解释层

    把 finalize 的硬数字压成事实摘要 → 交给 LLM 组织语言 → 回写 report.explanation。
    设计要点：
      - LLM 不参与任何计算，只消费已经算完的数字
      - 无 Key / 调用失败 / 任何异常 → 一律降级规则模板，绝不影响主流程
      - 输出侧做防幻觉回查，对不上的数字会被标记出来
    """
    if state.get("enable_explanation", True) is False:
        log.info("\n[LangGraph] ▶ explanation_node：已关闭（enable_explanation=False）")
        return {"explanation": "", "explanation_source": "none"}

    log.info("\n[LangGraph] ▶ explanation_node：LLM决策解释层")

    try:
        from src.agents.llm_explainer import (
            LLMExplainer, build_digest, template_explain,
        )

        report = state["report"]
        schedule = state["final_schedule"]
        price = state["price_profile"]

        # 基准对照：没传就现算（规则策略，非MILP，开销可忽略）
        baseline = state.get("baseline_result")
        if baseline is None:
            try:
                baseline = StorageOptimizationAgent(active_config()).baseline_strategy(
                    price, state["ambient_temp_profile"]
                )
            except Exception:
                baseline = None

        digest = build_digest(
            report=report,
            schedule=schedule,
            price_profile=price,
            baseline=baseline,
            dr_responses=state.get("dr_responses", []),
            ambient_temp=state.get("ambient_temp_profile"),
        )

        result = LLMExplainer().explain(digest)
        if result.used_llm and result.grounding is not None:
            g = result.grounding
            log.info(f"  LLM 解释生成完毕：{g.grounded}/{g.checked} 个数字通过事实回查")
            if g.unknown:
                log.info(f"  [提示] 未匹配数字：{', '.join(g.unknown[:5])}")
        else:
            log.info(f"  使用规则模板解释（source={result.source}"
                  + (f"，原因：{result.error[:80]}" if result.error else "") + ")")


        report.explanation = result.text
        report.explanation_source = result.source
        return {
            "digest": digest,
            "explanation": result.text,
            "explanation_source": result.source,
            "report": report,
        }

    except Exception as e:
        # 兜底：解释层永远不能把调度主流程拖挂
        log.warning(f"  [警告] 解释层异常，已跳过：{type(e).__name__}: {str(e)[:120]}")
        return {"explanation": "", "explanation_source": "none"}


# ========== 图构建 ==========

def build_scheduling_graph():
    """
    构建 LangGraph 调度图

    图结构：
        init → load_forecast → storage_optimization → dr_handler ⇄ dr_handler → finalize → END
                                          ↑________________________|
                                    （条件边：还有DR事件则循环）
    """
    workflow = StateGraph(SchedulingState)

    # 添加节点
    workflow.add_node("init", init_node)
    workflow.add_node("load_forecast", load_forecast_node)
    workflow.add_node("storage_optimization", storage_optimization_node)
    workflow.add_node("dr_handler", dr_handler_node)
    workflow.add_node("finalize", finalize_node)
    workflow.add_node("explanation", explanation_node)

    # 设置入口
    workflow.set_entry_point("init")

    # 线性边
    workflow.add_edge("init", "load_forecast")
    workflow.add_edge("load_forecast", "storage_optimization")
    workflow.add_edge("storage_optimization", "dr_handler")

    # 条件边：DR循环
    workflow.add_conditional_edges(
        "dr_handler",
        should_continue_dr,
        {
            "dr_handler": "dr_handler",   # 继续处理下一个DR事件
            "finalize": "finalize",       # 全部处理完，进入汇总
        },
    )

    # 结束边：finalize → explanation（LLM解释层）→ END
    workflow.add_edge("finalize", "explanation")
    workflow.add_edge("explanation", END)

    return workflow.compile()


# ========== 对外接口（与原 CoordinatorAgent 兼容） ==========

class LangGraphCoordinator:
    """
    LangGraph 版本调度协调器

    接口与 CoordinatorAgent 完全一致，可直接替换：
        coordinator = LangGraphCoordinator()
        report = coordinator.run_daily_scheduling(date, data, dr_signals)
    """

    def __init__(self, config=None):
        self.cfg = config or active_config()
        self.graph = build_scheduling_graph()
        self._last_state = None

    def run_daily_scheduling(
        self,
        date: str,
        historical_data: pd.DataFrame,
        dr_signals: Optional[List[DRSignal]] = None,
        use_ml_forecast: bool = True,
        include_thermal: bool = True,
        include_degradation: bool = True,
        xgb_params: Optional[Dict] = None,
        baseline: Optional[object] = None,
        enable_explanation: bool = True,
        progress_cb: Optional[callable] = None,
    ) -> DailyReport:
        """
        执行日调度全流程（LangGraph驱动）

        baseline: 预先算好的基准策略结果，传给解释层做对照（不传则解释层内部现算）
        enable_explanation: 是否启用 LLM 决策解释层（关掉可省一次 LLM 调用）
        progress_cb: 进度回调 progress_cb(node_name)，每完成一个图节点回调一次（供 UI 显示阶段进度）
        """
        log.info(f"\n{'='*60}")
        log.info(f"[LangGraph协调器] 开始日调度：{date}")
        log.info(f"{'='*60}")

        initial_state: SchedulingState = {
            "date": date,
            "historical_data": historical_data,
            "dr_signals": dr_signals or [],
            "use_ml_forecast": use_ml_forecast,
            "include_thermal": include_thermal,
            "include_degradation": include_degradation,
            "xgb_params": xgb_params,
            "baseline_result": baseline,
            "enable_explanation": enable_explanation,
        }

        if progress_cb is None:
            # 执行图（原路径，行为不变）
            final_state = self.graph.invoke(initial_state)
        else:
            # 带进度路径：updates 流回报节点名，values 流的最后一个分片即最终状态
            # （含 operator.add reducer 语义，不能手工合并 updates，必须取 values 快照）
            final_state = None
            for mode, payload in self.graph.stream(initial_state, stream_mode=["updates", "values"]):
                if mode == "updates":
                    for _node in (payload or {}):
                        if _node == "__end__":
                            continue
                        try:
                            progress_cb(_node)
                        except Exception:
                            pass  # 进度回调失败不影响求解
                else:
                    final_state = payload
            if final_state is None:  # 兜底：流异常时退回同步调用
                final_state = self.graph.invoke(initial_state)
        self._last_state = final_state

        return final_state["report"]

    def get_visualization_data(self) -> Dict:
        """获取可视化数据（供 Web 看板渲染）"""
        if self._last_state is None or "final_schedule" not in self._last_state:
            return {}

        state = self._last_state
        return {
            "time_index": state["time_index"],
            "price": state["price_profile"],
            "load_forecast": state["load_profile"],
            "load_actual": state["forecast_result"].actual_load_kw if state.get("forecast_result") else None,
            "charge_power": state["final_schedule"].charge_power_kw,
            "discharge_power": state["final_schedule"].discharge_power_kw,
            "net_power": state["final_schedule"].net_power_kw,
            "soc": state["final_schedule"].soc,
            "battery_temp": state["final_schedule"].battery_temp_c,
            "ambient_temp": state["ambient_temp_profile"],
            "base_schedule": state["base_schedule"],
            "final_schedule": state["final_schedule"],   # 给解释层用（含终值SOC/守恒残差）
            "dr_responses": state.get("dr_responses", []),
            "feature_importance": state.get("feature_importance", {}),
            "forecast_mape_raw": state["forecast_result"].mape_without_correction if state.get("forecast_result") else 0.0,
            "explanation": state.get("explanation", ""),
            "explanation_source": state.get("explanation_source", "none"),
        }

    def print_graph_structure(self):
        """打印图结构（调试用）"""
        log.info("LangGraph 调度图结构：")
        log.info("  init → load_forecast → storage_optimization → dr_handler ⇄ finalize → explanation → END")
        log.info("  条件边: dr_handler → dr_handler (还有DR事件) / finalize (处理完毕)")
        log.info("  explanation 节点：LLM决策解释层（无Key自动降级规则模板）")
