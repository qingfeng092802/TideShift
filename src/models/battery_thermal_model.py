"""
电池热模型（一阶RC集总参数法）

基于传热学集总参数法：
    C * dT/dt = I²R + Q_reaction - (T - T_amb) / R_th

其中：
- I²R：焦耳热（内阻发热）
- Q_reaction：电化学反应热（熵变热）
- (T - T_amb) / R_th：电池包到环境的对流散热

温度保护策略：
- T < 45℃：正常充放电
- 45℃ ≤ T < 55℃：限制充放电功率（降额运行）
- T ≥ 55℃：停止充放电
"""
import numpy as np
from dataclasses import dataclass
from src.utils.config import CONFIG, active_config


@dataclass
class ThermalState:
    """热状态"""
    temperature_c: float = 25.0       # 当前电池温度 ℃
    heat_generation_w: float = 0.0    # 当前产热功率 W
    heat_dissipation_w: float = 0.0   # 当前散热功率 W


class BatteryThermalModel:
    """电池一阶RC热模型"""

    def __init__(self, config=None):
        self.cfg = config or active_config().battery
        self.state = ThermalState()

    def reset(self, initial_temp: float = 25.0):
        """重置热状态"""
        self.state = ThermalState(temperature_c=initial_temp)

    def compute_heat_generation(self, power_kw: float, soc: float = 0.5) -> float:
        """
        计算产热功率（W）

        参数：
            power_kw: 充放电功率（正为放电，负为充电）kW
            soc: 当前SOC

        返回：
            产热功率 W
        """
        # 等效电流估算：I = P / U
        # 内阻由 (1-η)U²/P_rated 反推，故额定点 I²R 损耗 = P(1-η)，
        # 与 SOC 能量方程按 η 扣减的能量严格一致，不重复计损。
        nominal_voltage = self.cfg.nominal_voltage_v  # V
        current = abs(power_kw) * 1000 / nominal_voltage  # A

        # 焦耳热：I²R
        joule_heat = current ** 2 * self.cfg.internal_resistance_ohm  # W

        # 电化学反应热（熵变热，充放电时符号不同）
        # 充电时吸热（负），放电时放热（正），约为焦耳热的5-15%
        if power_kw > 0:  # 放电
            reaction_heat = joule_heat * self.cfg.reaction_heat_coeff
        else:  # 充电
            reaction_heat = -joule_heat * self.cfg.reaction_heat_coeff * 0.5

        total_heat = joule_heat + reaction_heat
        return total_heat

    def compute_heat_dissipation(self, battery_temp: float, ambient_temp: float) -> float:
        """
        计算散热功率（W）
        Q_diss = (T_battery - T_amb) / R_th
        """
        return (battery_temp - ambient_temp) / self.cfg.thermal_resistance_k_w

    def step(self, power_kw: float, ambient_temp: float,
             dt_hours: float = 0.25, soc: float = 0.5) -> float:
        """
        前进一步时间步，更新电池温度

        参数：
            power_kw: 充放电功率 kW（正放电，负充电）
            ambient_temp: 环境温度 ℃
            dt_hours: 时间步长 小时
            soc: 当前SOC

        返回：
            更新后的电池温度 ℃
        """
        dt_seconds = dt_hours * 3600

        # 计算产热和散热
        heat_gen = self.compute_heat_generation(power_kw, soc)
        heat_diss = self.compute_heat_dissipation(self.state.temperature_c, ambient_temp)

        # 净热量（J）= 功率(W) × 时间(s)
        net_heat_joules = (heat_gen - heat_diss) * dt_seconds

        # 温度变化：ΔT = Q / C（C单位 kJ/K = 1000 J/K）
        delta_t = net_heat_joules / (self.cfg.thermal_capacity_kj_k * 1000)

        # 更新温度
        new_temp = self.state.temperature_c + delta_t

        # 更新状态
        self.state.temperature_c = new_temp
        self.state.heat_generation_w = heat_gen
        self.state.heat_dissipation_w = heat_diss

        return new_temp

    def get_power_limit(self, battery_temp: float) -> float:
        """
        根据温度获取功率限制系数（0-1）

        返回：
            功率限制系数，1.0为满功率，0为停止
        """
        if battery_temp < self.cfg.temp_normal_max:
            return 1.0
        elif battery_temp < self.cfg.temp_safe_max:
            # 线性降额：45℃时100%，55℃时0%
            ratio = (self.cfg.temp_safe_max - battery_temp) / \
                    (self.cfg.temp_safe_max - self.cfg.temp_normal_max)
            return max(0.0, ratio)
        else:
            return 0.0

    def simulate_full_day(self, power_profile_kw: np.ndarray,
                          ambient_temp_profile: np.ndarray,
                          initial_temp: float = 25.0,
                          initial_soc: float = 0.5) -> dict:
        """
        仿真全天温度变化

        参数：
            power_profile_kw: 96点充放电功率曲线 kW（正放电，负充电）
            ambient_temp_profile: 96点环境温度曲线 ℃
            initial_temp: 初始电池温度
            initial_soc: 初始SOC

        返回：
            包含温度、产热、散热、功率限制的字典
        """
        self.reset(initial_temp)

        n = len(power_profile_kw)
        temps = np.zeros(n)
        heat_gens = np.zeros(n)
        heat_disss = np.zeros(n)
        power_limits = np.zeros(n)

        soc = initial_soc
        for i in range(n):
            # 原实现 temps[i] 记录的是**步进前**温度、
            # heat_gens[i] 是**上一步**残留产热（i=0 恒为 0），且末步 t=96 的温度
            # 从不进入统计 → 最高温漏检末点超温。现在：
            #   temps[i]  = 第 i 步起步温度（对应 power_profile_kw[i] 施加前的状态）
            #   heat_gens[i] = 本步（由 power_profile_kw[i] 产生）的产热
            #   max/min/over_temp 统计额外并入末步结束温度 self.state.temperature_c
            temps[i] = self.state.temperature_c
            power_limits[i] = self.get_power_limit(self.state.temperature_c)

            # 前进一步
            self.step(power_profile_kw[i], ambient_temp_profile[i],
                      dt_hours=0.25, soc=soc)

            # 记录本步的产热/散热（step() 更新 state 后的值即本步功率对应的值）
            heat_gens[i] = self.state.heat_generation_w
            heat_disss[i] = self.state.heat_dissipation_w

            # 简单更新SOC（用于热模型计算）
            if power_profile_kw[i] < 0:  # 充电
                soc += abs(power_profile_kw[i]) * 0.25 * self.cfg.charge_efficiency / self.cfg.rated_capacity_kwh
            else:  # 放电
                soc -= power_profile_kw[i] * 0.25 / (self.cfg.discharge_efficiency * self.cfg.rated_capacity_kwh)
            soc = np.clip(soc, 0, 1)

        # 末步结束温度纳入统计（此前 t=96 温度不进 max，漏检末点超温）
        final_temp = float(self.state.temperature_c)
        temps_ext = np.append(temps, final_temp)
        return {
            "temperature_c": temps,
            "heat_generation_w": heat_gens,
            "heat_dissipation_w": heat_disss,
            "power_limit_ratio": power_limits,
            "max_temperature_c": float(np.max(temps_ext)),
            "min_temperature_c": float(np.min(temps_ext)),
            "avg_temperature_c": float(np.mean(temps_ext)),
            "over_temp_count": int(np.sum(temps_ext >= self.cfg.temp_normal_max)),
        }


def estimate_temperature_rise(power_kw: float, duration_hours: float,
                              ambient_temp: float = 30.0,
                              initial_temp: float = 30.0) -> float:
    """
    快速估算持续充放电后的稳态温升（用于优化中的温度约束线性化）

    稳态时：产热 = 散热
    I²R + Q_reaction = (T - T_amb) / R_th
    => T_ss = T_amb + R_th * (I²R + Q_reaction)

    参数：
        power_kw: 充放电功率 kW
        duration_hours: 持续时间 小时
        ambient_temp: 环境温度 ℃
        initial_temp: 初始温度 ℃

    返回：
        预估温度 ℃
    """
    cfg = active_config().battery
    nominal_voltage = cfg.nominal_voltage_v
    current = abs(power_kw) * 1000 / nominal_voltage
    joule_heat = current ** 2 * cfg.internal_resistance_ohm
    # 原实现无条件 `+joule*coeff`，与 compute_heat_generation
    # （充电时反应热为 -0.5·coeff·joule）矛盾，充电场景高估热量约 1.5×coeff。
    # 现按功率符号区分：放电（>0）+coeff，充电（<0）-0.5·coeff，与 step() 同一套物理。
    if power_kw > 0:  # 放电：反应热为正
        reaction_heat = joule_heat * cfg.reaction_heat_coeff
    else:  # 充电：反应热为负（吸热）
        reaction_heat = -joule_heat * cfg.reaction_heat_coeff * 0.5
    total_heat = joule_heat + reaction_heat

    # 稳态温度
    steady_temp = ambient_temp + cfg.thermal_resistance_k_w * total_heat

    # 考虑时间常数的过渡过程
    # τ = C * R_th（热时间常数）
    tau = cfg.thermal_capacity_kj_k * 1000 * cfg.thermal_resistance_k_w  # 秒
    tau_hours = tau / 3600

    # 一阶惯性：T(t) = T_ss + (T0 - T_ss) * exp(-t/τ)
    if duration_hours > 0 and tau_hours > 0:
        factor = np.exp(-duration_hours / tau_hours)
    else:
        factor = 1.0

    estimated_temp = steady_temp + (initial_temp - steady_temp) * factor
    return estimated_temp
