"""
需求响应Agent

功能：接收电网削峰/填谷需求响应（DR）信号，动态调整充放电策略
在满足电网要求的同时最大化补贴收益，同时保证电池安全

能动专业核心壁垒：
1. 热安全校验：大功率放电导致电池温升加速，超温则降低响应功率
2. 用能底线校验：削峰响应不能低于用户生产工艺最低用电负荷
3. 收益校验：响应收益与电池衰减成本对比，净收益为正则响应
"""
import copy
from dataclasses import dataclass
from typing import Dict

import numpy as np
import pandas as pd

from src.models.battery_degradation_model import BatteryDegradationModel
from src.models.battery_thermal_model import BatteryThermalModel
from src.utils.config import active_config
from src.utils.logger import get_logger

# 原先这行夹在两组 import 之间（`log = get_logger(...)` 在第 13 行、其余 import
# 在它之后），ruff 的 E402 会为后面每一行报"模块级 import 不在文件顶部"。
log = get_logger(__name__)

# 此前 DR 里硬写两处 50（最低有效响应功率），config.price.dr_min_response_kw
# 声明后零引用。收敛为具名常量并注释语义（值保持 50 不变，避免行为变更）。
MIN_MEANINGFUL_RESPONSE_KW = 50.0  # 低于此功率的响应无商业价值，直接拒绝


@dataclass
class DRSignal:
    """需求响应信号"""
    start_time: str                  # 开始时间 "YYYY-MM-DD HH:MM"
    end_time: str                    # 结束时间
    target_reduction_kw: float       # 目标削减功率 kW
    subsidy_per_kwh: float           # 补贴单价 元/kWh
    dr_type: str = "peak_shaving"    # 类型：peak_shaving削峰 / valley_filling填谷


@dataclass
class DRResponse:
    """需求响应结果"""
    accepted: bool                   # 是否接受响应
    reason: str                      # 决策原因
    adjusted_power_kw: np.ndarray    # 调整后的充放电功率（正放电，负充电）
    response_power_kw: float         # 实际响应功率 kW
    response_energy_kwh: float       # 实际响应电量 kWh
    subsidy_revenue_yuan: float      # 补贴收益 元
    degradation_cost_yuan: float     # 额外衰减成本 元
    net_dr_revenue_yuan: float       # DR净收益 元
    max_temp_during_dr_c: float      # DR期间最高温度
    feasibility_check: Dict          # 可行性校验结果


class DemandResponseAgent:
    """需求响应Agent"""

    def __init__(self, config=None):
        # 此前 `config or CONFIG` 硬取全局 CONFIG——用户改 soc_max/rated_power
        # 后 MILP 遵守注入快照、DR 却按默认参数校验，可突破额定功率/超 SOC 放电。
        # 统一改 active_config()：求解线程内返回注入的配置快照，与 MILP 同源。
        self.cfg = config or active_config()
        self.battery_cfg = self.cfg.battery
        self.thermal_model = BatteryThermalModel(self.battery_cfg)
        self.degradation_model = BatteryDegradationModel(self.battery_cfg)

    def evaluate_and_respond(
        self,
        dr_signal: DRSignal,
        base_schedule_power: np.ndarray,
        current_soc: float,
        current_battery_temp: float,
        ambient_temp_profile: np.ndarray,
        load_profile: np.ndarray,
        time_index: pd.DatetimeIndex,
    ) -> DRResponse:
        """
        评估并响应需求响应信号

        流程：
        1. 解析DR信号，确定响应时段
        2. 三重可行性校验（热安全、用能底线、收益）
        3. 计算最优响应功率
        4. 生成调整后的策略

        注：第 2 步里的收益校验排在热/用能之后、且要等第 4 步的
        adjusted_power 出来才做——它需要用同一个衰减模型对"基础调度"与
        "响应后调度"两条 SOC 轨迹做差分，而目标功率会先被热安全与用能底线
        两次降额，只有最终功率才能反映真实收益。
        """
        # 拷贝输入信号，避免热降额/用能底线调整时污染调用方对象
        dr_signal = copy.deepcopy(dr_signal)

        dt = self.battery_cfg.time_step_hours

        # 1. 确定响应时段索引
        start_dt = pd.to_datetime(dr_signal.start_time)
        end_dt = pd.to_datetime(dr_signal.end_time)
        dr_mask = (time_index >= start_dt) & (time_index < end_dt)
        dr_indices = np.where(dr_mask)[0]

        if len(dr_indices) == 0:
            return DRResponse(
                accepted=False,
                reason="DR时段不在调度范围内",
                adjusted_power_kw=base_schedule_power.copy(),
                response_power_kw=0,
                response_energy_kwh=0,
                subsidy_revenue_yuan=0,
                degradation_cost_yuan=0,
                net_dr_revenue_yuan=0,
                max_temp_during_dr_c=current_battery_temp,
                feasibility_check={"error": "时段不匹配"},
            )

        feasibility = {}

        # 2. 热安全校验
        thermal_check = self._check_thermal_safety(
            dr_indices, base_schedule_power, current_soc,
            current_battery_temp, ambient_temp_profile,
            dr_signal.target_reduction_kw,
        )
        feasibility["thermal_safety"] = thermal_check

        if not thermal_check["safe"]:
            # 不直接拒绝，而是降低响应功率到安全水平（动态降额）
            safe_power = thermal_check["max_safe_power"]
            if safe_power > 50:  # 至少有50kW响应价值才接受
                original_target = dr_signal.target_reduction_kw
                dr_signal.target_reduction_kw = safe_power
                feasibility["thermal_safety"]["note"] = (
                    f"目标功率{original_target:.0f}kW会导致超温，"
                    f"自动降额至{safe_power:.0f}kW"
                )
                log.info(f"    [热安全] 自动降额：{original_target:.0f}kW → {safe_power:.0f}kW")
            else:
                return DRResponse(
                    accepted=False,
                    reason=f"热安全校验失败：预计温度{thermal_check['estimated_max_temp']:.1f}℃超过阈值，且安全响应功率不足",
                    adjusted_power_kw=base_schedule_power.copy(),
                    response_power_kw=0,
                    response_energy_kwh=0,
                    subsidy_revenue_yuan=0,
                    degradation_cost_yuan=0,
                    net_dr_revenue_yuan=0,
                    max_temp_during_dr_c=thermal_check["estimated_max_temp"],
                    feasibility_check=feasibility,
                )

        # 3. 用能底线校验
        energy_check = self._check_energy_floor(
            dr_indices, base_schedule_power, load_profile,
            dr_signal.target_reduction_kw,
        )
        feasibility["energy_floor"] = energy_check

        if not energy_check["feasible"]:
            # 降低响应功率到可行水平
            adjusted_target = energy_check["max_feasible_reduction"]
            dr_signal.target_reduction_kw = adjusted_target
            feasibility["energy_floor"]["note"] = f"目标削减功率过高，调整为{adjusted_target:.0f}kW"

        # 4. 计算最优响应功率（考虑热约束、SOC约束后的最大可响应功率）
        # SOC约束：DR期间额外放电量不能超过可用容量
        duration_hours = len(dr_indices) * dt
        # 计算base_schedule在DR期间已经计划的放电量
        base_discharge_during_dr = float(np.sum([
            max(0, base_schedule_power[i]) for i in dr_indices
        ]) * dt)
        # 可用能量 = 当前SOC到下限的能量 - base已计划放电量
        available_energy = (current_soc - self.battery_cfg.soc_min) * self.battery_cfg.rated_capacity_kwh
        available_energy -= base_discharge_during_dr / self.battery_cfg.discharge_efficiency
        available_energy = max(0, available_energy)
        # 考虑放电效率
        max_extra_discharge_energy = available_energy * self.battery_cfg.discharge_efficiency
        max_soc_power = max_extra_discharge_energy / duration_hours if duration_hours > 0 else 0

        feasibility["soc_constraint"] = {
            "current_soc": round(current_soc, 3),
            "base_discharge_during_dr_kwh": round(base_discharge_during_dr, 1),
            "available_energy_kwh": round(available_energy, 1),
            "max_soc_power_kw": round(max_soc_power, 1),
        }

        max_response_power = min(
            thermal_check["max_safe_power"],
            dr_signal.target_reduction_kw,
            self.battery_cfg.rated_power_kw,
            max_soc_power,
        )

        if max_response_power < MIN_MEANINGFUL_RESPONSE_KW:
            return DRResponse(
                accepted=False,
                reason=f"SOC不足：当前SOC={current_soc:.1%}，可用能量不足以支持有效响应",
                adjusted_power_kw=base_schedule_power.copy(),
                response_power_kw=0,
                response_energy_kwh=0,
                subsidy_revenue_yuan=0,
                degradation_cost_yuan=0,
                net_dr_revenue_yuan=0,
                max_temp_during_dr_c=current_battery_temp,
                feasibility_check=feasibility,
            )

        # 5. 生成调整后的策略
        adjusted_power = base_schedule_power.copy()
        for idx in dr_indices:
            if dr_signal.dr_type == "peak_shaving":
                # 削峰：增加放电功率（或减少充电）
                current_power = adjusted_power[idx]  # 正为放电
                # 目标：净放电功率增加 target_reduction
                target_power = current_power + max_response_power
                # 限制在额定功率内
                target_power = min(target_power, self.battery_cfg.rated_power_kw)
                adjusted_power[idx] = target_power
            else:
                # 填谷：增加充电功率
                current_power = adjusted_power[idx]
                target_power = current_power - max_response_power
                target_power = max(target_power, -self.battery_cfg.rated_power_kw)
                adjusted_power[idx] = target_power

        # 5. 收益校验
        # 排在这里而不是热/用能校验之间：只有拿到了真正要执行的 adjusted_power，
        # 才能用与 MILP 目标函数同一个衰减模型，对"基础调度"与"响应后调度"
        # 两条 SOC 轨迹做差分。原先这一步排在前面（那时 adjusted_power 还不存在），
        # 只能用 target_reduction_kw × 0.8（假设响应率）× 1.5（硬编码区间系数）
        # 另立一套估算——同一份衰减在 MILP、DR 判定、最终报表里竟有三种算法，
        # 且 DR 判定这套用的常数优化器根本看不见。
        revenue_check = self._check_dr_revenue(
            dr_indices, dr_signal, base_schedule_power, adjusted_power, current_soc,
        )
        feasibility["revenue"] = revenue_check

        if not revenue_check["profitable"]:
            return DRResponse(
                accepted=False,
                reason=f"收益校验失败：DR净收益为负（{revenue_check['net_revenue']:.2f}元）",
                adjusted_power_kw=base_schedule_power.copy(),
                response_power_kw=0,
                response_energy_kwh=0,
                subsidy_revenue_yuan=0,
                degradation_cost_yuan=0,
                net_dr_revenue_yuan=0,
                max_temp_during_dr_c=thermal_check["estimated_max_temp"],
                feasibility_check=feasibility,
            )

        # 6. 汇总响应量与收益（数值已在 _check_dr_revenue 里算好，不再重复一次差分）
        response_power = revenue_check["response_power_kw"]
        response_energy = revenue_check["response_energy_kwh"]
        subsidy_revenue = revenue_check["estimated_subsidy"]
        extra_degradation_cost = revenue_check["estimated_degradation"]
        net_dr_revenue = revenue_check["net_revenue"]

        # DR期间最高温度
        dr_temps = thermal_check["temp_profile"][dr_indices] if "temp_profile" in thermal_check else []
        max_temp_dr = float(np.max(dr_temps)) if len(dr_temps) > 0 else current_battery_temp

        return DRResponse(
            accepted=True,
            reason="响应成功",
            adjusted_power_kw=adjusted_power,
            response_power_kw=round(response_power, 1),
            response_energy_kwh=round(response_energy, 1),
            subsidy_revenue_yuan=round(subsidy_revenue, 2),
            degradation_cost_yuan=round(extra_degradation_cost, 2),
            net_dr_revenue_yuan=round(net_dr_revenue, 2),
            max_temp_during_dr_c=round(max_temp_dr, 1),
            feasibility_check=feasibility,
        )

    def _check_thermal_safety(
        self, dr_indices, base_power, current_soc, current_temp,
        ambient_temp, target_reduction,
    ) -> Dict:
        """热安全校验"""
        dt = self.battery_cfg.time_step_hours

        # 估算DR期间的最大功率
        max_power_during_dr = max(
            np.max(np.abs(base_power[dr_indices])) + target_reduction,
            self.battery_cfg.rated_power_kw * 0.5,
        )

        # 这里原先还调了一次 estimate_temperature_rise 算"稳态温升"，
        # 但结果从未被读过——下面就用热模型逐步仿真出整条温度剖面了，
        # 稳态估算被完全取代。删掉这次白算（它在每个 DR 事件里都跑一遍）。
        # 需要稳态口径时直接用 estimate_temperature_rise，不必在这里留一份死值。

        # 更精确的仿真
        self.thermal_model.reset(current_temp)
        temp_profile = np.zeros(len(base_power))
        soc = current_soc
        for i in range(len(base_power)):
            temp_profile[i] = self.thermal_model.state.temperature_c
            power = base_power[i]
            if i in dr_indices:
                power += target_reduction  # 增加放电
            self.thermal_model.step(power, ambient_temp[i], dt, soc)
            if power < 0:
                soc += abs(power) * dt * self.battery_cfg.charge_efficiency / self.battery_cfg.rated_capacity_kwh
            else:
                soc -= power * dt / (self.battery_cfg.discharge_efficiency * self.battery_cfg.rated_capacity_kwh)
            soc = np.clip(soc, 0, 1)

        # 热惯性 τ≈4.2h：DR 窗口结束后温度仍可能继续攀升——实测 DR 后 48.9℃ 越过了
        # 45℃ 降额线。因此校验对象是全天温度剖面，而不只是 DR 窗口内那几个点。
        window_max_temp = float(np.max(temp_profile[dr_indices])) if len(dr_indices) > 0 else current_temp
        max_temp = float(np.max(temp_profile)) if len(temp_profile) > 0 else current_temp
        overshoot_note = ""
        if max_temp > window_max_temp + 0.5:  # 0.5℃ 内的尾巴属正常热惯性，不算过冲
            _peak_i = int(np.argmax(temp_profile))
            overshoot_note = (f"DR窗口后热惯性过冲：全天最高{max_temp:.1f}℃（窗口内{window_max_temp:.1f}℃），"
                              f"峰值出现在第{_peak_i}个时段（窗口外）")

        # 计算安全功率上限
        safe = max_temp < self.battery_cfg.temp_normal_max
        max_safe_power = self.battery_cfg.rated_power_kw
        if not safe:
            # 反推安全功率（简化：线性插值）
            temp_margin = self.battery_cfg.temp_normal_max - current_temp
            if temp_margin > 0:
                max_safe_power = max_power_during_dr * (temp_margin / (max_temp - current_temp)) * 0.8
            else:
                max_safe_power = 0

        return {
            "safe": safe,
            "estimated_max_temp": round(max_temp, 1),
            "window_max_temp": round(window_max_temp, 1),
            "overshoot_note": overshoot_note,
            "threshold": self.battery_cfg.temp_normal_max,
            "max_safe_power": round(max_safe_power, 1),
            "temp_profile": temp_profile,
        }

    def _check_energy_floor(
        self, dr_indices, base_power, load_profile, target_reduction,
    ) -> Dict:
        """用能底线校验：削峰后净负荷不能低于工艺最低负荷"""
        # 净负荷 = 原始负荷 - 储能放电 + 储能充电
        # 削峰增加放电，会降低净负荷
        min_load = self.cfg.load.min_process_load_kw

        net_load_after = load_profile[dr_indices] - target_reduction
        min_net_load = float(np.min(net_load_after))

        feasible = min_net_load >= min_load
        max_feasible = float(np.min(load_profile[dr_indices] - min_load))

        return {
            "feasible": feasible,
            "min_net_load_after": round(min_net_load, 1),
            "min_process_load": min_load,
            "max_feasible_reduction": round(max(max_feasible, 0), 1),
        }

    def _check_dr_revenue(
        self, dr_indices, dr_signal, base_power, adjusted_power, current_soc,
    ) -> Dict:
        """收益校验：DR补贴收益 > 额外衰减成本。

        衰减成本不再是估算常数，而是把"基础调度"与"响应后调度"两条功率曲线
        分别喂给 `BatteryDegradationModel`，取两者 `degradation_cost_yuan` 之差。
        这样 DR 判定用的就是 MILP 目标函数里那一套（`unit_cost =
        total_cost / (2 × Cap × cycle_life)` + SOC 区间加权），三处口径合一：
        优化器认为划算的方案，DR 判定不会再因为另一套常数把它拒掉。

        参数：
            dr_indices: DR 时段在 96 点序列里的下标
            dr_signal: DR 信号（已按热/用能底线降额）
            base_power: 基础调度功率（正放电负充电）
            adjusted_power: 响应后功率
            current_soc: 当前 SOC（两条轨迹的同一初值）
        """
        dt = self.battery_cfg.time_step_hours

        response_power = float(np.mean([
            adjusted_power[i] - base_power[i] for i in dr_indices
        ]))
        response_energy = float(np.sum([
            abs(adjusted_power[i] - base_power[i]) for i in dr_indices
        ]) * dt)
        subsidy_revenue = response_energy * dr_signal.subsidy_per_kwh

        base_deg = self.degradation_model.compute_degradation_from_power(
            base_power, initial_soc=current_soc
        )
        adj_deg = self.degradation_model.compute_degradation_from_power(
            adjusted_power, initial_soc=current_soc
        )
        # 显式 float()：模型的 degradation_cost_yuan 是 np.float64，
        # 不转的话 net_revenue / profitable 会分别变成 np.float64 / np.bool_，
        # 而 np.bool_ 不是 bool 的子类——它们会一路渗进 feasibility_check
        # 并被缓存序列化，进而出现在接口返回值里（numpy 标量不是 JSON 原生类型）。
        degradation_cost = float(
            adj_deg.degradation_cost_yuan - base_deg.degradation_cost_yuan
        )

        net_revenue = subsidy_revenue - degradation_cost
        profitable = bool(net_revenue > 0)

        return {
            "profitable": profitable,
            "estimated_subsidy": round(subsidy_revenue, 2),
            "estimated_degradation": round(degradation_cost, 2),
            "net_revenue": round(net_revenue, 2),
            # 下面两项一并返回，避免调用方为了写 DRResponse 再把两条 SOC 轨迹
            # 差分一遍（差分才是这里唯一有成本的部分）。取值精度与
            # DRResponse 对应字段一致，故调用方再 round 一次是幂等的。
            "response_power_kw": round(response_power, 1),
            "response_energy_kwh": round(response_energy, 1),
        }
