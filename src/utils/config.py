"""
全局配置参数
工商业储能系统：1MW/2MWh 磷酸铁锂
电价参考：广东省工商业峰谷分时电价（2024年典型水平）
"""
import threading
from dataclasses import dataclass, field, replace
from typing import Dict, Tuple

import numpy as np


@dataclass(frozen=True)
class BatteryConfig:
    """电池系统参数（1MW/2MWh 磷酸铁锂）"""
    # 额定参数
    rated_power_kw: float = 1000.0        # 额定功率 kW
    rated_capacity_kwh: float = 2000.0    # 额定容量 kWh

    # SOC约束
    soc_min: float = 0.20                 # 最低SOC 20%
    soc_max: float = 0.90                 # 最高SOC 90%
    soc_initial: float = 0.50             # 初始SOC

    # 效率
    charge_efficiency: float = 0.95       # 充电效率
    discharge_efficiency: float = 0.95    # 放电效率
    # 自放电率（每15分钟）。当前 SOC 能量方程未纳入自放电（量级 0.05%/步，
    # 对日调度结果影响 <0.1%），保留字段供模型扩展时启用，启用前勿删除。
    self_discharge_rate: float = 0.0005

    # 标称电压（用于 I = P / U 折算电流）
    nominal_voltage_v: float = 600.0      # V

    # 热模型参数（一阶RC集总参数法）
    # 取值依据见 docs/PARAMETERS.md，三个参数的量级均按 2MWh 集装箱标定：
    #   内阻：由充放电效率反推，使额定点 I²R 损耗 = P(1-η)，避免与 SOC 能量方程重复计损
    #   热容：约 13~20 吨电芯 × ~1 kJ/(kg·K)，含机架箱体取 15000 kJ/K
    #   热阻：2MWh 集装箱自然对流+风机约 1 kW/K（不含空调，代表空调失效的最坏校核工况）
    thermal_capacity_kj_k: float = 15000.0   # 电池系统热容 kJ/K
    thermal_resistance_k_w: float = 0.001    # 热阻 K/W（到环境，等效散热能力 1.0 kW/K）
    internal_resistance_ohm: float = 0.018   # 内阻 ohm（由 (1-η)·U²/P_rated 反推）
    reaction_heat_coeff: float = 0.05     # 电化学反应热系数（占焦耳热比例）

    # 温度阈值
    temp_normal_max: float = 45.0         # 正常工作上限 ℃
    temp_safe_max: float = 55.0           # 安全停止上限 ℃
    # 危险温度（≥ 此值触发硬停机告警）。当前两级降额只用到上面两档，
    # 第三档保留供告警/停机链路扩展时启用，启用前勿删除。
    temp_critical: float = 60.0           # 危险温度 ℃

    # 寿命衰减模型
    # 不同SOC区间的等效衰减系数（深充深放是浅充浅放的2-3倍）
    degradation_coeff: Dict[str, float] = field(default_factory=lambda: {
        "0.0-0.2": 3.0,   # 极低SOC区，衰减大
        "0.2-0.5": 1.5,
        "0.5-0.8": 1.0,   # 最佳区间
        "0.8-1.0": 2.0,   # 高SOC区，衰减较大
    })
    cycle_life: int = 6000                # 标称循环寿命（80%DoD下）
    battery_cost_per_kwh: float = 800.0   # 电池成本 元/kWh
    total_battery_cost: float = 1600000.0  # 系统总成本 元

    # 时间粒度
    time_step_hours: float = 0.25         # 15分钟 = 0.25小时
    num_steps: int = 96                   # 一天96个点


@dataclass(frozen=True)
class PriceConfig:
    """电价配置（广东省工商业峰谷分时电价，元/kWh）"""
    # 尖峰电价（10-12月、1-2月的11:00-12:00, 15:00-17:00, 19:00-21:00）
    spike_price: float = 1.35
    # 高峰电价（8:00-11:00, 13:00-15:00, 17:00-19:00, 21:00-23:00）
    peak_price: float = 1.05
    # 平段电价（7:00-8:00, 12:00-13:00, 23:00-24:00）
    flat_price: float = 0.65
    # 低谷电价（0:00-7:00）
    valley_price: float = 0.32

    # 需求响应补贴
    dr_subsidy_per_kwh: float = 0.8       # 削峰补贴 元/kWh
    # 最小有效响应功率。DR Agent 内以 MIN_MEANINGFUL_RESPONSE_KW=50 常量引用
    # （见 demand_response_agent.py）；此字段为配置化预留，后续可让 DR Agent 读取此值。
    dr_min_response_kw: float = 100.0     # 最小响应功率


@dataclass(frozen=True)
class LoadConfig:
    """负荷配置"""
    # 典型工商业日负荷基准（kW），96点
    # 模拟一个中型工厂+办公混合负荷
    base_load_kw: float = 1500.0
    # 温度-负荷修正系数（夏季）
    temp_load_coeff_commercial: float = 0.05  # 商业建筑：每℃ 5%
    temp_load_coeff_industrial: float = 0.025 # 工业制冷：每℃ 2.5%
    # 工艺最低负荷（不可低于此值）
    min_process_load_kw: float = 800.0


@dataclass(frozen=True)
class SystemConfig:
    """系统总配置"""
    battery: BatteryConfig = field(default_factory=BatteryConfig)
    price: PriceConfig = field(default_factory=PriceConfig)
    load: LoadConfig = field(default_factory=LoadConfig)

    # 环境温度（夏季典型日，96点）
    ambient_temp_c: float = 30.0           # 基准环境温度

    # 优化目标权重
    weight_arbitrage: float = 1.0          # 套利收益权重
    weight_degradation: float = 1.0        # 衰减成本权重
    weight_dr: float = 1.0                 # 需求响应收益权重


# 全局单例（修复：frozen 不可变，任何位置不得直接赋值属性）
# 求解时通过 use_config() 注入线程级配置快照，读取方一律用 active_config()，
# 保证并发求解参数互不污染、页面线程看到的永远是干净的默认全局配置。
CONFIG = SystemConfig()

_ACTIVE = threading.local()


def active_config() -> "SystemConfig":
    """当前线程生效的配置：求解线程返回注入的快照，其他线程返回全局默认。"""
    cfg = getattr(_ACTIVE, "cfg", None)
    return cfg if cfg is not None else CONFIG


from contextlib import contextmanager


@contextmanager
def use_config(cfg: "SystemConfig"):
    """在当前线程内临时生效一份配置快照（不可变），退出自动还原。"""
    _ACTIVE.cfg = cfg
    try:
        yield cfg
    finally:
        _ACTIVE.cfg = None


def internal_resistance_from_efficiency(efficiency: float, nominal_voltage_v: float,
                                        rated_power_kw: float) -> float:
    """
    由充放电效率反推等效内阻，使额定功率点的 I²R 损耗恰好等于 P(1-η)。

    推导：I = P/U，令 I²R = P(1-η)
          => (P/U)² · R = P(1-η)
          => R = (1-η) · U² / P      （P 取瓦特）

    这样热模型与 SOC 能量方程在额定点严格自洽：
    SOC 方程按 η 扣减的能量，恰好就是热模型算出来的那部分热，不重复计损。
    """
    return (1.0 - efficiency) * nominal_voltage_v ** 2 / (rated_power_kw * 1000.0)


def heat_pwl_breakpoints(cfg: BatteryConfig, num_segments: int = 6):
    """
    生成产热曲线 P_heat = k·P² 的分段线性（chord）断点，用于 MILP 中的线性化。

    返回 (p_points, q_points)：功率断点 kW 与对应产热 W。

    为什么用 chord（弦）而不是内插：
    P² 是凸函数，弦位于曲线**上方**，即在任意区间内 chord(P) >= k·P²。
    温度约束形如 T <= T_max，T 关于产热单调增，用高估的产热得到的温度也偏高，
    因此约束是**保守安全**的：满足 chord 约束 => 一定满足真实物理约束。
    """
    k = cfg.internal_resistance_ohm * (1 + cfg.reaction_heat_coeff) / cfg.nominal_voltage_v ** 2
    # k 的量纲：产热(W) = k · P(W)²，这里 P 用 kW 传入，故换算
    # Q(W) = I²R = (P_kw*1000/U)² · R  =>  k_kw = R·1e6/U²
    k_kw = cfg.internal_resistance_ohm * (1 + cfg.reaction_heat_coeff) * 1e6 / cfg.nominal_voltage_v ** 2
    p_points = np.linspace(0.0, cfg.rated_power_kw, num_segments + 1)
    q_points = k_kw * p_points ** 2
    return p_points, q_points
