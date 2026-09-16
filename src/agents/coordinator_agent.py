"""
调度协调Agent（多智能体中枢）

功能：
1. 任务分发：按顺序调用负荷预测→储能调度→需求响应
2. 状态同步：在各Agent间传递数据
3. 结果汇总：统一输出最终调度结果与能效报表
4. 异常处理：负荷预测偏差过大、电池超温时触发应急策略

实现方式：
- 默认使用纯Python工作流编排（轻量、可控、无需额外依赖）
- 可选使用LangGraph编排（需安装langgraph）
"""
from src.utils.logger import get_logger
log = get_logger(__name__)
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from datetime import datetime

from src.utils.config import CONFIG, active_config
from src.agents.load_forecast_agent import LoadForecastAgent, ForecastResult
from src.agents.storage_optimization_agent import StorageOptimizationAgent, ScheduleResult
from src.agents.demand_response_agent import DemandResponseAgent, DRSignal, DRResponse


@dataclass
class SystemState:
    """系统状态（在各Agent间传递）"""
    # 输入数据
    date: str = ""
    historical_data: Optional[pd.DataFrame] = None
    price_profile: Optional[np.ndarray] = None
    ambient_temp_profile: Optional[np.ndarray] = None
    load_profile: Optional[np.ndarray] = None
    time_index: Optional[pd.DatetimeIndex] = None

    # 各Agent输出
    forecast_result: Optional[ForecastResult] = None
    base_schedule: Optional[ScheduleResult] = None
    dr_signals: List[DRSignal] = field(default_factory=list)
    dr_responses: List[DRResponse] = field(default_factory=list)
    final_schedule: Optional[ScheduleResult] = None

    # 系统状态
    current_soc: float = 0.5
    current_battery_temp: float = 25.0
    alerts: List[str] = field(default_factory=list)


@dataclass
class DailyReport:
    """日能效报表"""
    date: str
    arbitrage_revenue_yuan: float
    dr_subsidy_yuan: float
    degradation_cost_yuan: float
    net_revenue_yuan: float
    charge_energy_kwh: float
    discharge_energy_kwh: float
    max_battery_temp_c: float
    equivalent_cycles: float
    forecast_mape: float
    dr_events_count: int
    alerts: List[str]
    # v1.2：LLM 决策解释层输出（给默认空值，保持向后兼容）
    explanation: str = ""
    explanation_source: str = "none"   # llm / rule / none

    def to_dict(self) -> dict:
        return {
            "日期": self.date,
            "峰谷套利收益(元)": round(self.arbitrage_revenue_yuan, 2),
            "需求响应补贴(元)": round(self.dr_subsidy_yuan, 2),
            "电池衰减成本(元)": round(self.degradation_cost_yuan, 2),
            "净收益(元)": round(self.net_revenue_yuan, 2),
            "充电量(kWh)": round(self.charge_energy_kwh, 1),
            "放电量(kWh)": round(self.discharge_energy_kwh, 1),
            "最高电池温度(℃)": round(self.max_battery_temp_c, 1),
            "等效循环次数": round(self.equivalent_cycles, 4),
            "负荷预测MAPE(%)": round(self.forecast_mape, 2),
            "DR响应次数": self.dr_events_count,
            "告警信息": "; ".join(self.alerts) if self.alerts else "无",
        }


class CoordinatorAgent:
    """调度协调Agent（纯 Python 编排）

    🟡#32 说明：与 langgraph_coordinator.LangGraphCoordinator 是同一调度流水线的
    两套编排器（本类有状态、LangGraph 版无状态）。测试与「编排引擎=纯Python」
    选项仍依赖本类；新功能请优先加在 LangGraph 版并在本类保持接口对齐。
    长期方向：合并为单一编排（见审查报告阶段3建议 #16）。
    """

    def __init__(self, config=None, xgb_params=None):
        self.cfg = config or active_config()
        self.load_agent = LoadForecastAgent(self.cfg, xgb_params=xgb_params)
        self.storage_agent = StorageOptimizationAgent(self.cfg)
        self.dr_agent = DemandResponseAgent(self.cfg)
        self.state = SystemState()

    def run_daily_scheduling(
        self,
        date: str,
        historical_data: pd.DataFrame,
        dr_signals: Optional[List[DRSignal]] = None,
        use_ml_forecast: bool = True,
        include_thermal: bool = True,
        include_degradation: bool = True,
    ) -> DailyReport:
        """
        执行日调度全流程

        流程：
        1. 负荷预测Agent → 输出次日96点负荷曲线
        2. 储能优化调度Agent → 输出基准充放电计划
        3. 需求响应Agent → 动态修正策略
        4. 汇总输出能效报表
        """
        log.info(f"\n{'='*60}")
        log.info(f"[调度协调Agent] 开始日调度：{date}")
        log.info(f"{'='*60}")

        # 初始化状态
        self.state = SystemState(
            date=date,
            historical_data=historical_data,
            dr_signals=dr_signals or [],
        )

        # 获取当日数据
        day_data = historical_data[
            historical_data["timestamp"].dt.date == pd.to_datetime(date).date()
        ].reset_index(drop=True)

        if len(day_data) == 0:
            # 如果没有当日数据，用预测
            self.state.price_profile = self._generate_price_profile(date)
            self.state.ambient_temp_profile = np.ones(96) * 30.0
            self.state.time_index = pd.date_range(start=date, periods=96, freq="15min")
        else:
            self.state.price_profile = day_data["price_yuan_per_kwh"].values
            self.state.ambient_temp_profile = day_data["ambient_temp_c"].values
            self.state.load_profile = day_data["load_kw"].values
            self.state.time_index = day_data["timestamp"].values

        # ===== 阶段1：负荷预测 =====
        log.info("\n[阶段1/4] 负荷预测Agent运行中...")
        try:
            if use_ml_forecast:
                forecast = self.load_agent.predict(
                    historical_data, date, apply_physical_correction=True
                )
            else:
                forecast = self.load_agent.predict_simple(
                    historical_data, date, apply_physical_correction=True
                )
            self.state.forecast_result = forecast
            self.state.load_profile = forecast.forecast_load_kw

            log.info(f"  预测完成，MAPE: {forecast.mape}%")
            log.info(f"  物理修正幅度: {forecast.correction_magnitude_kw:.1f} kW")

            # 预测偏差告警
            if forecast.mape > 15:
                alert = f"负荷预测偏差过大（MAPE={forecast.mape}%），建议增加实时修正频率"
                self.state.alerts.append(alert)
                log.info(f"  [告警] {alert}")
            # 降级告警：非空表示本次未走 XGBoost 主路径，必须让用户看到口径变化，
            # 而不是在报表上默默展示一组来源不同的数字。
            if getattr(forecast, "fallback_reason", ""):
                alert = f"负荷预测已降级：{forecast.fallback_reason}"
                self.state.alerts.append(alert)
                log.warning(f"  [告警] {alert}")
        except Exception as e:
            log.warning(f"  [警告] 负荷预测失败({e})，使用历史均值替代")
            forecast = self.load_agent.predict_simple(historical_data, date)
            self.state.forecast_result = forecast
            self.state.load_profile = forecast.forecast_load_kw
            # 🟠 降级路径同样要落告警：此前异常分支不写 alerts，报表上看不出本轮
            # 用的不是 XGBoost 而是历史均值，MAPE 口径也就无从辨认。
            self.state.alerts.append(
                f"负荷预测异常，已降级为历史均值预测（{type(e).__name__}）：{str(e)[:120]}")

        # ===== 阶段2：储能优化调度 =====
        log.info("\n[阶段2/4] 储能优化调度Agent运行中...")
        base_schedule = self.storage_agent.optimize(
            price_profile=self.state.price_profile,
            load_profile=self.state.load_profile,
            ambient_temp_profile=self.state.ambient_temp_profile,
            initial_soc=self.state.current_soc,
            include_thermal_constraint=include_thermal,
            include_degradation_cost=include_degradation,
        )
        self.state.base_schedule = base_schedule

        log.info(f"  求解状态: {base_schedule.solver_status}")
        log.info(f"  基准套利收益: {base_schedule.arbitrage_revenue_yuan:.2f} 元")
        log.info(f"  基准衰减成本: {base_schedule.degradation_cost_yuan:.2f} 元")
        log.info(f"  最高温度: {base_schedule.max_battery_temp_c:.1f} ℃")

        # 超温告警
        if base_schedule.max_battery_temp_c > self.cfg.battery.temp_normal_max:
            alert = f"电池温度超过正常上限（{base_schedule.max_battery_temp_c:.1f}℃），已触发降额"
            self.state.alerts.append(alert)
            log.info(f"  [告警] {alert}")

        # ===== 阶段3：需求响应 =====
        log.info("\n[阶段3/4] 需求响应Agent运行中...")
        final_power = base_schedule.net_power_kw.copy()
        total_dr_subsidy = 0
        total_dr_degradation = 0
        dr_count = 0

        for dr_signal in self.state.dr_signals:
            log.info(f"  处理DR事件: {dr_signal.start_time} - {dr_signal.end_time}, "
                  f"目标削减{dr_signal.target_reduction_kw:.0f}kW")


            # 确定DR时段开始时的电池温度和SOC（基于基准调度结果）
            start_dt = pd.to_datetime(dr_signal.start_time)
            time_idx_pd = pd.to_datetime(self.state.time_index)
            dr_start_mask = time_idx_pd >= start_dt
            if dr_start_mask.any():
                dr_start_idx = int(np.argmax(dr_start_mask))
                # 用基准调度中DR开始时刻的温度和SOC
                dr_initial_temp = base_schedule.battery_temp_c[dr_start_idx] if dr_start_idx < len(base_schedule.battery_temp_c) else self.state.current_battery_temp
                dr_initial_soc = base_schedule.soc[dr_start_idx] if dr_start_idx < len(base_schedule.soc) else self.state.current_soc
            else:
                dr_initial_temp = self.state.current_battery_temp
                dr_initial_soc = self.state.current_soc

            dr_response = self.dr_agent.evaluate_and_respond(
                dr_signal=dr_signal,
                base_schedule_power=final_power,
                current_soc=dr_initial_soc,
                current_battery_temp=dr_initial_temp,
                ambient_temp_profile=self.state.ambient_temp_profile,
                load_profile=self.state.load_profile,
                time_index=pd.to_datetime(self.state.time_index),
            )

            self.state.dr_responses.append(dr_response)

            if dr_response.accepted:
                final_power = dr_response.adjusted_power_kw
                total_dr_subsidy += dr_response.subsidy_revenue_yuan
                total_dr_degradation += dr_response.degradation_cost_yuan
                dr_count += 1
                log.info(f"    ✓ 接受响应，补贴{dr_response.subsidy_revenue_yuan:.2f}元，"
                      f"实际响应{dr_response.response_power_kw:.0f}kW")

            else:
                log.warning(f"    ✗ 拒绝响应：{dr_response.reason}")

        log.info(f"  DR事件处理完成：接受{dr_count}/{len(self.state.dr_signals)}个，"
              f"总补贴{total_dr_subsidy:.2f}元")


        # ===== 阶段4：结果汇总 =====
        log.info("\n[阶段4/4] 汇总最终结果...")

        # SOC后处理：确保DR调整后的功率满足SOC约束（物理可行性裁剪）
        dt = self.cfg.battery.time_step_hours
        soc = self.state.current_soc
        for i in range(len(final_power)):
            power = final_power[i]
            if power > 0:  # 放电
                max_discharge = (soc - self.cfg.battery.soc_min) * self.cfg.battery.rated_capacity_kwh / dt * self.cfg.battery.discharge_efficiency
                final_power[i] = min(power, max(0, max_discharge))
                soc -= final_power[i] * dt / (self.cfg.battery.discharge_efficiency * self.cfg.battery.rated_capacity_kwh)
            elif power < 0:  # 充电
                max_charge = (self.cfg.battery.soc_max - soc) * self.cfg.battery.rated_capacity_kwh / dt / self.cfg.battery.charge_efficiency
                final_power[i] = max(power, -max(0, max_charge))
                soc += abs(final_power[i]) * dt * self.cfg.battery.charge_efficiency / self.cfg.battery.rated_capacity_kwh
            soc = np.clip(soc, self.cfg.battery.soc_min, self.cfg.battery.soc_max)

        # 基于最终功率重新计算收益和温度
        charge_power = np.where(final_power < 0, -final_power, 0)
        discharge_power = np.where(final_power > 0, final_power, 0)

        charge_energy = np.sum(charge_power) * dt
        discharge_energy = np.sum(discharge_power) * dt
        arbitrage_rev = float(np.sum(
            (discharge_power * self.state.price_profile -
             charge_power * self.state.price_profile) * dt
        ))

        # 温度仿真
        thermal_result = self.storage_agent.thermal_model.simulate_full_day(
            final_power, self.state.ambient_temp_profile,
            initial_temp=self.state.ambient_temp_profile[0],
            initial_soc=self.state.current_soc,
        )

        # 衰减计算
        deg_result = self.storage_agent.degradation_model.compute_degradation_from_power(
            final_power, initial_soc=self.state.current_soc
        )

        total_degradation_cost = deg_result.degradation_cost_yuan
        net_revenue = arbitrage_rev + total_dr_subsidy - total_degradation_cost

        # 生成最终调度结果
        final_schedule = ScheduleResult(
            charge_power_kw=charge_power,
            discharge_power_kw=discharge_power,
            net_power_kw=final_power,
            soc=deg_result.soc_trajectory if hasattr(deg_result, 'soc_trajectory') else base_schedule.soc,
            battery_temp_c=thermal_result["temperature_c"],
            arbitrage_revenue_yuan=round(arbitrage_rev, 2),
            degradation_cost_yuan=round(total_degradation_cost, 2),
            net_revenue_yuan=round(net_revenue, 2),
            charge_energy_kwh=round(charge_energy, 1),
            discharge_energy_kwh=round(discharge_energy, 1),
            max_battery_temp_c=round(thermal_result["max_temperature_c"], 1),
            equivalent_cycles=round(deg_result.equivalent_cycles, 4),
            solver_status="Final_DR_Adjusted",
        )
        self.state.final_schedule = final_schedule

        # 生成日报表
        report = DailyReport(
            date=date,
            arbitrage_revenue_yuan=arbitrage_rev,
            dr_subsidy_yuan=total_dr_subsidy,
            degradation_cost_yuan=total_degradation_cost,
            net_revenue_yuan=net_revenue,
            charge_energy_kwh=charge_energy,
            discharge_energy_kwh=discharge_energy,
            max_battery_temp_c=thermal_result["max_temperature_c"],
            equivalent_cycles=deg_result.equivalent_cycles,
            forecast_mape=self.state.forecast_result.mape if self.state.forecast_result else 0,
            dr_events_count=dr_count,
            alerts=self.state.alerts.copy(),
        )

        log.info(f"\n{'='*60}")
        log.info(f"[调度协调Agent] 日调度完成")
        log.info(f"{'='*60}")
        log.info(f"  峰谷套利收益: {report.arbitrage_revenue_yuan:.2f} 元")
        log.info(f"  需求响应补贴: {report.dr_subsidy_yuan:.2f} 元")
        log.info(f"  电池衰减成本: {report.degradation_cost_yuan:.2f} 元")
        log.info(f"  净收益:       {report.net_revenue_yuan:.2f} 元")
        log.info(f"  最高电池温度: {report.max_battery_temp_c:.1f} ℃")
        log.info(f"  等效循环:     {report.equivalent_cycles:.4f} 次")

        return report

    def _generate_price_profile(self, date: str) -> np.ndarray:
        """生成典型日电价曲线"""
        from src.data.data_generator import generate_price_profile
        time_idx = pd.date_range(start=date, periods=96, freq="15min")
        return generate_price_profile(time_idx)

    def get_visualization_data(self) -> Dict:
        """获取可视化数据（供Streamlit使用）"""
        if self.state.final_schedule is None:
            return {}

        return {
            "time_index": self.state.time_index,
            "price": self.state.price_profile,
            "load_forecast": self.state.load_profile,
            "load_actual": self.state.forecast_result.actual_load_kw if self.state.forecast_result else None,
            "charge_power": self.state.final_schedule.charge_power_kw,
            "discharge_power": self.state.final_schedule.discharge_power_kw,
            "net_power": self.state.final_schedule.net_power_kw,
            "soc": self.state.final_schedule.soc,
            "battery_temp": self.state.final_schedule.battery_temp_c,
            "ambient_temp": self.state.ambient_temp_profile,
            "base_schedule": self.state.base_schedule,
            "final_schedule": self.state.final_schedule,   # v1.2：给解释层用
            "dr_responses": self.state.dr_responses,
            "feature_importance": self.load_agent.get_feature_importance(),
            "forecast_mape_raw": self.state.forecast_result.mape_without_correction if self.state.forecast_result else 0.0,
        }
