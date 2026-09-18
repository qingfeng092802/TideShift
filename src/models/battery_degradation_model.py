"""
电池寿命衰减模型

基于：
1. SOC区间衰减系数：深充深放衰减是浅充浅放的2-3倍
2. 循环寿命：6000次（80%DoD下）
3. 衰减成本折算：每次充放电的寿命损耗 × 电池总成本

核心思路：
- 将每次充放电的能量按SOC区间加权，得到等效循环次数
- 等效循环次数 / 总循环寿命 = 寿命损耗比例
- 寿命损耗比例 × 电池总成本 = 衰减成本
"""
import numpy as np
from dataclasses import dataclass
from typing import List, Tuple
from src.utils.config import CONFIG, active_config


@dataclass
class DegradationResult:
    """衰减计算结果"""
    equivalent_cycles: float          # 等效循环次数
    degradation_ratio: float          # 寿命衰减比例
    degradation_cost_yuan: float      # 衰减成本 元
    soc_trajectory: np.ndarray        # SOC轨迹
    throughput_kwh: float             # 总充放电吞吐量 kWh


class BatteryDegradationModel:
    """电池寿命衰减模型"""

    def __init__(self, config=None):
        self.cfg = config or active_config().battery

    def get_soc_degradation_coeff(self, soc: float) -> float:
        """
        获取当前SOC区间的衰减系数

        SOC区间划分：
        - 0.0-0.2: 极低区，衰减系数3.0（锂析出风险高）
        - 0.2-0.5: 中低区，衰减系数1.5
        - 0.5-0.8: 最佳区，衰减系数1.0
        - 0.8-1.0: 高区，衰减系数2.0（固体电解质界面膜增长快）
        """
        coeffs = self.cfg.degradation_coeff
        if soc < 0.2:
            return coeffs["0.0-0.2"]
        elif soc < 0.5:
            return coeffs["0.2-0.5"]
        elif soc < 0.8:
            return coeffs["0.5-0.8"]
        else:
            return coeffs["0.8-1.0"]

    def compute_degradation_from_soc_trajectory(
        self, soc_trajectory: np.ndarray, time_step_hours: float = 0.25
    ) -> DegradationResult:
        """
        基于SOC轨迹计算寿命衰减

        方法：
        1. 计算每个时间步的SOC变化量（ΔSOC）
        2. 对每个ΔSOC，按该区间的衰减系数加权
        3. 加权总吞吐量 / 额定容量 = 等效循环次数
        4. 等效循环次数 / 标称循环寿命 = 衰减比例
        5. 衰减比例 × 电池总成本 = 衰减成本

        参数：
            soc_trajectory: SOC轨迹数组（包含初始和最终状态）
            time_step_hours: 时间步长

        返回：
            DegradationResult
        """
        if len(soc_trajectory) < 2:
            return DegradationResult(0, 0, 0, soc_trajectory, 0)

        # SOC 轨迹含 NaN 时行为未定义（比较恒 False → 衰减系数错取区间）。
        # 显式校验，坏数据早暴露。
        if not np.isfinite(soc_trajectory).all():
            raise ValueError("SOC 轨迹含 NaN/Inf，无法计算寿命衰减——请检查上游充放电功率曲线")

        # 计算每个时间步的SOC变化
        delta_soc = np.diff(soc_trajectory)
        abs_delta_soc = np.abs(delta_soc)

        # 计算每个时间步中点的SOC，用于确定衰减系数
        soc_midpoints = (soc_trajectory[:-1] + soc_trajectory[1:]) / 2

        # 按SOC区间加权
        weighted_delta = np.zeros_like(abs_delta_soc)
        for i in range(len(abs_delta_soc)):
            coeff = self.get_soc_degradation_coeff(soc_midpoints[i])
            weighted_delta[i] = abs_delta_soc[i] * coeff

        # 等效循环次数 = 加权总ΔSOC / 2（一次完整循环=充+放）
        # 标称循环寿命基于80% DoD，这里归一化到100% DoD等效
        total_weighted_soc = np.sum(weighted_delta)
        equivalent_cycles = total_weighted_soc / 2.0

        # 实际吞吐量（kWh）
        throughput_kwh = np.sum(abs_delta_soc) * self.cfg.rated_capacity_kwh

        # 衰减比例 = 等效循环次数 / 标称循环寿命
        # 注意：标称循环寿命是80%DoD下的，等效到100%DoD需要修正
        # 经验：80%DoD循环6000次 ≈ 100%DoD循环约4000次（衰减非线性）
        # 简化处理：直接用等效循环次数 / 标称寿命，偏保守
        degradation_ratio = equivalent_cycles / self.cfg.cycle_life

        # 衰减成本
        degradation_cost = degradation_ratio * self.cfg.total_battery_cost

        return DegradationResult(
            equivalent_cycles=equivalent_cycles,
            degradation_ratio=degradation_ratio,
            degradation_cost_yuan=degradation_cost,
            soc_trajectory=soc_trajectory,
            throughput_kwh=throughput_kwh,
        )

    def compute_degradation_from_power(
        self, power_profile_kw: np.ndarray,
        initial_soc: float = 0.5,
        time_step_hours: float = 0.25,
    ) -> DegradationResult:
        """
        基于充放电功率曲线计算衰减

        参数：
            power_profile_kw: 充放电功率 kW（正放电，负充电）
            initial_soc: 初始SOC
            time_step_hours: 时间步长

        返回：
            DegradationResult
        """
        n = len(power_profile_kw)
        soc = np.zeros(n + 1)
        soc[0] = initial_soc

        for i in range(n):
            power = power_profile_kw[i]
            if power < 0:  # 充电
                energy_in = abs(power) * time_step_hours * self.cfg.charge_efficiency
                soc[i + 1] = soc[i] + energy_in / self.cfg.rated_capacity_kwh
            else:  # 放电
                energy_out = power * time_step_hours / self.cfg.discharge_efficiency
                soc[i + 1] = soc[i] - energy_out / self.cfg.rated_capacity_kwh

            # 限制在物理范围内（按配置的 soc_min/soc_max 裁剪而非 0~1，
            # 0~1 口径会把 soc_min=0.2 以下的越界也当"正常"，掩盖真实越界）
            soc[i + 1] = np.clip(soc[i + 1], self.cfg.soc_min, self.cfg.soc_max)

        return self.compute_degradation_from_soc_trajectory(soc, time_step_hours)

    def estimate_degradation_cost(
        self, charge_energy_kwh: float, discharge_energy_kwh: float,
        avg_soc: float = 0.5, soc_range: Tuple[float, float] = (0.2, 0.9)
    ) -> float:
        """
        快速估算衰减成本（用于优化目标函数）

        参数：
            charge_energy_kwh: 充电能量 kWh
            discharge_energy_kwh: 放电能量 kWh
            avg_soc: 平均SOC
            soc_range: SOC运行区间 (min, max)

        返回：
            衰减成本 元
        """
        # 等效循环次数（取充放电的较大值）
        throughput = max(charge_energy_kwh, discharge_energy_kwh)
        dod = soc_range[1] - soc_range[0]  # 放电深度
        # soc_range 上下限相等时除零 → ZeroDivisionError/inf。显式拒绝非法区间。
        if dod <= 1e-9:
            raise ValueError(f"soc_range 上下限相等（{soc_range}），DoD 为零无法折算等效循环")
        equivalent_cycles = throughput / (self.cfg.rated_capacity_kwh * dod)

        # SOC区间衰减系数（取平均）
        coeff = self.get_soc_degradation_coeff(avg_soc)

        # 加权等效循环
        weighted_cycles = equivalent_cycles * coeff

        # 衰减成本
        degradation_ratio = weighted_cycles / self.cfg.cycle_life
        return degradation_ratio * self.cfg.total_battery_cost


def calculate_lifetime_extension(
    optimized_cycles_per_year: float,
    baseline_cycles_per_year: float,
    cycle_life: int = 6000,
) -> dict:
    """
    计算寿命延长比例

    参数：
        optimized_cycles_per_year: 优化策略年等效循环次数
        baseline_cycles_per_year: 基准策略年等效循环次数
        cycle_life: 标称循环寿命

    返回：
        包含寿命年限、延长比例的字典
    """
    baseline_years = cycle_life / baseline_cycles_per_year if baseline_cycles_per_year > 0 else 0
    optimized_years = cycle_life / optimized_cycles_per_year if optimized_cycles_per_year > 0 else 0

    if baseline_years > 0:
        extension_ratio = (optimized_years - baseline_years) / baseline_years
    else:
        extension_ratio = 0

    return {
        "baseline_lifetime_years": round(baseline_years, 2),
        "optimized_lifetime_years": round(optimized_years, 2),
        "extension_ratio": round(extension_ratio, 4),
        "extension_percent": round(extension_ratio * 100, 1),
    }
