"""
储能优化调度Agent（项目核心）

基于混合整数线性规划（MILP）的充放电优化调度
目标：最大化净收益 = 峰谷套利收益 + 需求响应补贴 - 电池衰减成本

三层约束：
1. 电气约束：SOC上下限、充放电功率、效率、不能同时充放
2. 经济约束：峰谷电价时段
3. 热与寿命约束（能动专业核心壁垒）：
   - 电池温升约束（一阶RC热模型 + 产热二次项分段线性化）
   - SOC区间加权寿命衰减成本纳入目标函数

v1.1 修复记录（2026-09-07）：
  [P0-2] 热模型三个参数量级校准：内阻由效率反推、热阻/热容按2MWh集装箱量级取值
  [P0-3] 产热 I²R 是二次项，原实现错误地写成线性；改为 SOS2 分段线性（弦）上逼近，
         保证 MILP 内热约束与后验温度仿真一致（弦高估产热 → 约束保守安全）
  [P1-1] 新增终值SOC约束，默认循环稳态（SOC_end = SOC_start），消除"吃初始SOC老本"
  [P1-2] 基准策略重写：谷段充满 + 尖峰放完，严格能量守恒，不再用 np.clip 掩盖越界
  [P1-4] 寿命衰减从"常数1.3"改为真正的 SOC 区间加权，优化器会主动避开深充深放
"""
import numpy as np
import pandas as pd
import pulp
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from src.utils.config import CONFIG, active_config, heat_pwl_breakpoints, internal_resistance_from_efficiency
from src.utils.logger import get_logger
from src.models.battery_thermal_model import estimate_temperature_rise, BatteryThermalModel
from src.models.battery_degradation_model import BatteryDegradationModel

_log = get_logger("storage_optimization")

# 产热曲线分段线性化的段数
HEAT_PWL_SEGMENTS = 6


@dataclass
class ScheduleResult:
    """调度结果"""
    charge_power_kw: np.ndarray       # 充电功率 kW（电网侧）
    discharge_power_kw: np.ndarray    # 放电功率 kW（电网侧）
    net_power_kw: np.ndarray          # 净功率（放电为正）kW
    soc: np.ndarray                   # SOC轨迹（含初始值，长度97）
    battery_temp_c: np.ndarray        # 电池温度曲线（后验仿真）
    arbitrage_revenue_yuan: float     # 套利收益 元
    degradation_cost_yuan: float      # 衰减成本 元（后验 SOC 区间加权）
    net_revenue_yuan: float           # 净收益 元
    charge_energy_kwh: float          # 总充电量 kWh（电网侧）
    discharge_energy_kwh: float       # 总放电量 kWh（电网侧）
    max_battery_temp_c: float         # 最高电池温度
    equivalent_cycles: float          # 等效循环次数
    solver_status: str                # 求解器状态
    # v1.1 新增
    terminal_soc: float = 0.0              # 终值SOC（稳态口径下 = 初始SOC）
    energy_balance_error_kwh: float = 0.0  # 能量守恒残差 kWh（应≈0）
    dr_revenue_yuan: float = 0.0           # 需求响应补贴 元
    soc_violation_steps: int = 0           # SOC越界步数（正确实现下应为0）
    degradation_in_objective_yuan: float = 0.0  # 优化目标中实际计入的衰减成本
    # 🔴#5 新增：求解质量字段（透传到前端显著标注，杜绝"静默垃圾解"）
    time_limit_hit: bool = False           # 是否命中求解时限（得到的是次优解）
    mip_gap_pct: float = 0.0               # 求解时的相对最优间隙容差（%）


class StorageOptimizationAgent:
    """储能优化调度Agent"""

    def __init__(self, config=None):
        self.cfg = config or active_config()
        self.battery_cfg = self.cfg.battery
        self.thermal_model = BatteryThermalModel(self.battery_cfg)
        self.degradation_model = BatteryDegradationModel(self.battery_cfg)

    # ------------------------------------------------------------------ #
    #  主优化入口
    # ------------------------------------------------------------------ #
    def optimize(
        self,
        price_profile: np.ndarray,
        load_profile: Optional[np.ndarray] = None,
        ambient_temp_profile: Optional[np.ndarray] = None,
        initial_soc: float = 0.5,
        include_thermal_constraint: bool = True,
        include_degradation_cost: bool = True,
        dr_signal: Optional[Dict] = None,
        terminal_soc: Optional[float] = "cyclic",
        soc_weighted_degradation: bool = True,
        time_limit_s: int = 120,
        mip_gap: float = 0.01,
    ) -> ScheduleResult:
        """
        执行优化调度

        参数：
            price_profile: 96点电价曲线 元/kWh
            load_profile: 96点负荷曲线 kW（预留，当前模型为价格驱动）
            ambient_temp_profile: 96点环境温度曲线 ℃
            initial_soc: 初始SOC
            include_thermal_constraint: 是否包含热约束
            include_degradation_cost: 是否包含衰减成本
            dr_signal: 需求响应信号 {start_idx, end_idx, target_reduction_kw, subsidy_per_kwh}
            terminal_soc: 终值SOC。"cyclic"（默认）= 等于 initial_soc，代表可持续的日循环；
                          传 None 表示不加约束（单日口径，会吃掉初始SOC，不可持续）
            soc_weighted_degradation: True=按SOC区间加权（深充深放惩罚更高，优化器会主动规避）
                                      False=退化为常数系数近似（求解更快）
            time_limit_s: 求解时限

        返回：
            ScheduleResult
        """
        n = self.battery_cfg.num_steps  # 96
        dt = self.battery_cfg.time_step_hours  # 0.25h
        cap = self.battery_cfg.rated_capacity_kwh
        pmax = self.battery_cfg.rated_power_kw
        eta_c = self.battery_cfg.charge_efficiency
        eta_d = self.battery_cfg.discharge_efficiency

        if ambient_temp_profile is None:
            ambient_temp_profile = np.ones(n) * self.cfg.ambient_temp_c

        # 终值SOC：默认与初值一致，保证日循环可持续
        if terminal_soc == "cyclic":
            terminal_soc = initial_soc

        # ========== 1. 创建问题 ==========
        prob = pulp.LpProblem("EnergyStorageScheduling", pulp.LpMaximize)

        # ========== 2. 决策变量 ==========
        P_charge = [pulp.LpVariable(f"P_charge_{t}", lowBound=0, upBound=pmax) for t in range(n)]
        P_discharge = [pulp.LpVariable(f"P_discharge_{t}", lowBound=0, upBound=pmax) for t in range(n)]
        SOC = [pulp.LpVariable(f"SOC_{t}", lowBound=self.battery_cfg.soc_min,
                               upBound=self.battery_cfg.soc_max) for t in range(n + 1)]

        # ========== 3. 目标函数 ==========
        arbitrage_revenue = pulp.lpSum([
            (P_discharge[t] * price_profile[t] - P_charge[t] * price_profile[t]) * dt
            for t in range(n)
        ])

        dr_revenue = 0
        if dr_signal is not None:
            start_idx, end_idx = dr_signal["start_idx"], dr_signal["end_idx"]
            subsidy = dr_signal["subsidy_per_kwh"]
            dr_revenue = pulp.lpSum([
                P_discharge[t] * subsidy * dt for t in range(start_idx, end_idx)
            ])

        # ---- 寿命衰减成本 ----
        # 电池侧吞吐（kWh）：充电存入 P·dt·ηc，放电取出 P·dt/ηd
        thr = [P_charge[t] * dt * eta_c + P_discharge[t] * dt / eta_d for t in range(n)]
        # 单位加权吞吐的衰减成本：total_cost / (2 × Cap × cycle_life)
        # 与 BatteryDegradationModel 中 equivalent_cycles = Σcoeff·|Δsoc| / 2 完全一致
        unit_cost = self.battery_cfg.total_battery_cost / (2.0 * cap * self.battery_cfg.cycle_life)

        degradation_expr = 0
        if include_degradation_cost:
            if soc_weighted_degradation:
                degradation_expr = self._build_soc_weighted_degradation(
                    prob, SOC, thr, n, unit_cost)
            else:
                coeffs = self.battery_cfg.degradation_coeff
                avg_coeff = float(np.mean(list(coeffs.values())))
                degradation_expr = pulp.lpSum([thr[t] * avg_coeff * unit_cost for t in range(n)])

        prob += arbitrage_revenue + dr_revenue - degradation_expr

        # ========== 4. 约束条件 ==========
        # 4.1 初始 / 终值 SOC
        prob += SOC[0] == initial_soc
        if terminal_soc is not None:
            prob += SOC[n] == terminal_soc

        # 4.2 SOC动态方程
        for t in range(n):
            prob += SOC[t + 1] == (
                SOC[t]
                + P_charge[t] * dt * eta_c / cap
                - P_discharge[t] * dt / (eta_d * cap)
            )

        # 4.3 功率上限 & 防止同时充放电
        # 不再用 96 个二进制变量 u 互斥，改为一条线性约束。依据：
        # 若某时段同时 P_ch>0 且 P_dis>0，把二者同减 min(P_ch, P_dis)：
        #   - 收益项 (P_dis - P_ch)·price 不变
        #   - 衰减项 (P_ch·ηc + P_dis/ηd)·cost 严格变小（更好）
        #   - SOC 轨迹完全不变
        # 故"同时充放"严格劣于等效净功率，最优解不会取它。
        # 省掉 96 个二进制后模型显著变小，求解更快。
        for t in range(n):
            prob += P_charge[t] + P_discharge[t] <= pmax

        # 4.4 热约束（一阶RC + 产热二次项分段线性化）
        if include_thermal_constraint:
            self._add_thermal_constraints(
                prob, P_charge, P_discharge, ambient_temp_profile, n, dt)

        # 4.5 需求响应约束
        if dr_signal is not None:
            start_idx, end_idx = dr_signal["start_idx"], dr_signal["end_idx"]
            # 🟡#30 修复：DR 硬约束加可行性兜底——目标削减超过额定功率时整个 MILP 会
            # Infeasible 且上层从不检查。此处把要求钳到 pmax（留 0.1% 余量防数值边界），
            # 并记录告警，让"目标物理上做不到"显性化而不是把求解器逼死。
            dr_req_kw = float(dr_signal["target_reduction_kw"]) * 0.8
            if dr_req_kw > pmax * 0.999:
                _log.warning("DR目标%.0fkW×0.8超过额定功率%.0fkW，已钳制到额定值（原目标不可行）",
                             dr_signal["target_reduction_kw"], pmax)
                dr_req_kw = pmax * 0.999
            for t in range(start_idx, end_idx):
                prob += P_discharge[t] >= dr_req_kw

        # ========== 5. 求解 ==========
        # 优先 HiGHS（比 CBC 快一个量级），不可用时回退 CBC（回退必打 WARNING，🟠#16）
        status, solver_status, time_limit_hit = self._solve(prob, time_limit_s, mip_gap)

        # 🔴#5 修复：求解状态必须校验，杜绝"静默产出垃圾解"。
        # Infeasible 时解不存在——此前 pulp.value() 返回 None 直接构造 object 数组，
        # np.clip 抛错或静默失真。现在显式抛错并携带可读信息。
        if solver_status == "Infeasible":
            raise ValueError(
                "MILP 求解结果不可行（Infeasible）：请检查 DR 目标功率是否超过额定功率、"
                "SOC 区间/终值约束是否自洽、热约束是否过严")
        if solver_status in ("Undefined", "Not Solved"):
            # 有可行 incumbent（CBC/HiGHS 超时但已找到可行解）→ 降级为次优解继续；
            # 完全无解 → 显式报错，绝不静默用垃圾解
            _has_incumbent = any(pulp.value(P_charge[t]) is not None for t in range(n))
            if _has_incumbent:
                _log.warning("MILP 未证明最优（status=%s, 时限=%ss），使用可行次优解并显著标注",
                             solver_status, time_limit_s)
                solver_status = "Stopped (time limit, feasible incumbent)"
                time_limit_hit = True
            else:
                raise ValueError(
                    f"MILP 未能求出任何可行解（状态: {solver_status}）：时限 {time_limit_s}s 内"
                    "模型未找到可行解，请放宽求解时限（time_limit_s）或减小约束强度")
        elif solver_status != "Optimal":
            # Stopped 等状态（时限/ gap 提前停止但拿到可行解）：可用但必须显著标注为次优
            _log.warning("MILP 非最优解：status=%s（时限=%ss, gap=%s%%），结果为次优解",
                         solver_status, time_limit_s, mip_gap * 100)
            time_limit_hit = True

        # ========== 6. 提取结果 ==========
        charge_power = np.array([pulp.value(P_charge[t]) or 0.0 for t in range(n)])
        discharge_power = np.array([pulp.value(P_discharge[t]) or 0.0 for t in range(n)])
        # 🔴#5 修复：此行原本是全函数唯一没写 `or 0.0` 的提取——求解异常时 value() 返回
        # None，np.array 得到 object 数组，后续 np.clip 抛错或静默失真
        soc_values = np.array([pulp.value(SOC[t]) or 0.0 for t in range(n + 1)])

        charge_power = np.maximum(charge_power, 0)
        discharge_power = np.maximum(discharge_power, 0)
        soc_values = np.clip(soc_values, 0, 1)

        net_power = discharge_power - charge_power

        charge_energy = float(np.sum(charge_power) * dt)
        discharge_energy = float(np.sum(discharge_power) * dt)
        arbitrage_rev = float(np.sum((discharge_power * price_profile - charge_power * price_profile) * dt))

        # 后验仿真电池温度（与 MILP 内同一套一阶RC模型）
        thermal_result = self.thermal_model.simulate_full_day(
            net_power, ambient_temp_profile,
            initial_temp=ambient_temp_profile[0],
            initial_soc=initial_soc,
        )
        battery_temp = thermal_result["temperature_c"]
        max_temp = thermal_result["max_temperature_c"]

        # 后验衰减（SOC区间加权，与优化目标同源）
        deg_result = self.degradation_model.compute_degradation_from_power(
            net_power, initial_soc=initial_soc, time_step_hours=dt)
        degradation_cost_actual = deg_result.degradation_cost_yuan
        equivalent_cycles = deg_result.equivalent_cycles

        dr_rev = 0.0
        if dr_signal is not None:
            dr_rev = float(np.sum(discharge_power[dr_signal["start_idx"]:dr_signal["end_idx"]])
                           * dt * dr_signal["subsidy_per_kwh"])

        net_rev = arbitrage_rev + dr_rev - degradation_cost_actual

        # ---- 能量守恒自检：SOC轨迹 vs 充放电吞吐 ----
        soc_recomputed, violations = self._replay_soc(charge_power, discharge_power, initial_soc)
        balance_err = float(np.max(np.abs(soc_recomputed - soc_values[:n + 1])) * cap)

        return ScheduleResult(
            charge_power_kw=charge_power,
            discharge_power_kw=discharge_power,
            net_power_kw=net_power,
            soc=soc_values,
            battery_temp_c=battery_temp,
            arbitrage_revenue_yuan=round(arbitrage_rev, 2),
            degradation_cost_yuan=round(degradation_cost_actual, 2),
            net_revenue_yuan=round(net_rev, 2),
            charge_energy_kwh=round(charge_energy, 2),
            discharge_energy_kwh=round(discharge_energy, 2),
            max_battery_temp_c=round(max_temp, 2),
            equivalent_cycles=round(equivalent_cycles, 4),
            solver_status=solver_status,
            terminal_soc=round(float(soc_values[-1]), 4),
            energy_balance_error_kwh=round(balance_err, 4),
            dr_revenue_yuan=round(dr_rev, 2),
            soc_violation_steps=int(violations),
            degradation_in_objective_yuan=round(float(pulp.value(degradation_expr) or 0.0), 2),
            time_limit_hit=bool(time_limit_hit),
            mip_gap_pct=round(mip_gap * 100, 3),
        )

    # ------------------------------------------------------------------ #
    #  求解器选择
    # ------------------------------------------------------------------ #
    @staticmethod
    def _solve(prob, time_limit_s: int, mip_gap: float = 0.005):
        """
        优先用 HiGHS（对这类带大量二进制的 MILP 通常比 CBC 快一个量级），
        不可用时回退 CBC。返回 (status_code, status_str, time_limit_hit)。

        mip_gap：相对最优间隙容差。日调度场景 0.5% 的收益误差完全可接受，
        但能把求解时间从分钟级压到秒级。

        🟠#16 修复：HiGHS 依赖（highspy）此前未声明，抛异常被 except 静默吞掉，
        每次都回退 CBC 而"快一个量级"从未生效。现在回退必打 WARNING 日志。
        """
        time_limit_hit = False
        try:
            status = prob.solve(pulp.HiGHS(msg=0, timeLimit=float(time_limit_s),
                                            gapRel=mip_gap))
            status_str = pulp.LpStatus[status]
            if status_str not in ("Undefined", "Not Solved"):
                if status_str != "Optimal":
                    time_limit_hit = True  # 命中时限拿到可行解（非最优证明）
                return status, status_str, time_limit_hit
        except Exception:
            # 🟠#16：回退必须可见——依赖未装/求解器崩溃不能无痕吞掉
            _log.warning("HiGHS 求解不可用，回退 CBC（pip install highspy 可启用高速求解器）",
                         exc_info=True)
        try:
            status = prob.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit_s,
                                                   gapRel=mip_gap))
        except TypeError:
            status = prob.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit_s))
        status_str = pulp.LpStatus[status]
        if status_str != "Optimal" and status_str not in ("Undefined", "Not Solved"):
            time_limit_hit = True
        return status, status_str, time_limit_hit

    # ------------------------------------------------------------------ #
    #  热约束：一阶RC + 产热二次项 SOS2 分段线性化
    # ------------------------------------------------------------------ #
    def _add_thermal_constraints(self, prob, P_charge, P_discharge,
                                 ambient_temp_profile, n, dt):
        """
        加入温度递推与温度上限约束。

        物理：C·dT/dt = Q_gen(P) - (T - T_amb)/R_th，其中 Q_gen = I²R(1+k) ∝ P²

        关键修复：产热是功率的**二次**函数，MILP 里必须用分段线性处理。
        这里采用 SOS2 + 弦（chord）逼近：
          P² 是凸函数，弦位于曲线上方 → 用弦算出的产热偏高 → 推算温度偏高
          → 约束 T_chord <= T_max 蕴含 T_true <= T_max，即**保守安全**。
        """
        cfg = self.battery_cfg
        C_J = cfg.thermal_capacity_kj_k * 1000.0      # J/K
        R_th = cfg.thermal_resistance_k_w
        T_max = cfg.temp_normal_max

        p_pts, q_pts = heat_pwl_breakpoints(cfg, HEAT_PWL_SEGMENTS)
        n_seg = len(p_pts) - 1

        T_batt = [pulp.LpVariable(f"T_batt_{t}", lowBound=0, upBound=90) for t in range(n + 1)]
        prob += T_batt[0] == ambient_temp_profile[0]

        heat_coeff = dt * 3600.0 / C_J              # K per J
        diss_coeff = dt * 3600.0 / (R_th * C_J)     # 每步散热比例

        for t in range(n):
            # 该时段实际功率（充放互斥，故 P_ch + P_dis 即实际功率）
            P_t = P_charge[t] + P_discharge[t]

            # SOS2 权重
            lam = [pulp.LpVariable(f"lam_{t}_{i}", lowBound=0, upBound=1) for i in range(n_seg + 1)]
            y = [pulp.LpVariable(f"y_{t}_{j}", cat="Binary") for j in range(n_seg)]

            prob += pulp.lpSum(lam) == 1
            prob += pulp.lpSum([lam[i] * p_pts[i] for i in range(n_seg + 1)]) == P_t
            prob += pulp.lpSum(y) == 1
            # SOS2 邻接约束：只有相邻两个 lambda 可以非零
            prob += lam[0] <= y[0]
            for i in range(1, n_seg):
                prob += lam[i] <= y[i - 1] + y[i]
            prob += lam[n_seg] <= y[n_seg - 1]

            Q_t = pulp.lpSum([lam[i] * q_pts[i] for i in range(n_seg + 1)])  # W，产热（弦，偏高）

            prob += T_batt[t + 1] == (
                T_batt[t]
                + heat_coeff * Q_t
                - diss_coeff * (T_batt[t] - ambient_temp_profile[t])
            )
            prob += T_batt[t] <= T_max

        prob += T_batt[n] <= T_max

    # ------------------------------------------------------------------ #
    #  SOC 区间加权衰减（真正把深充深放惩罚写进优化目标）
    # ------------------------------------------------------------------ #
    def _build_soc_weighted_degradation(self, prob, SOC, thr, n, unit_cost):
        """
        把 SOC 区间加权衰减写成 MILP 可处理的形式。

        4 个 SOC 区间（与 BatteryDegradationModel.get_soc_degradation_coeff 一致），
        每时段用二进制 z_k[t] 标记该时段中点 SOC 落在哪个区间，
        再把该时段吞吐 thr[t] 拆分到 4 个变量 thr_k[t]，按区间系数加权计费。

        这样优化器能"看见"：在 0.8~1.0 区间吞吐的成本是 0.5~0.8 区间的 2 倍，
        于是会主动避开高 SOC 深充与低 SOC 深放 —— 这才是 README 声称的那个卖点。
        """
        coeffs = self.battery_cfg.degradation_coeff
        all_bounds = [(0.0, 0.2, coeffs["0.0-0.2"]),
                      (0.2, 0.5, coeffs["0.2-0.5"]),
                      (0.5, 0.8, coeffs["0.5-0.8"]),
                      (0.8, 1.0, coeffs["0.8-1.0"])]
        # 只保留 SOC 实际可能落入的区间（SOC 被限制在 [soc_min, soc_max]），
        # SOC=0.2~0.9 只跨 3 段，比固定用 4 段少 96 个二进制
        lo_lim, hi_lim = self.battery_cfg.soc_min, self.battery_cfg.soc_max
        bounds = [b for b in all_bounds if b[1] > lo_lim + 1e-9 and b[0] < hi_lim - 1e-9]
        if not bounds:
            bounds = all_bounds
        K = len(bounds)
        # 🟠#29 修复：big-M 原来取 rated_capacity_kwh=2000，但这两组约束的值域远小于此：
        #   - mid ∈ [soc_min, soc_max]，mid>=lo-M(1-z) 只需 M ≥ lo-soc_min（≤ 区间宽度）
        #   - thr_k ≤ M·z 只需 M ≥ 单步最大吞吐（pmax·dt·(ηc+1/ηd) ≈ 500）
        # M=2000 使 LP 松弛极弱 → 界差大、分支多，是 864 个二进制逼近 120s 时限的
        # 直接原因之一。按值域收紧后 LP 界显著变紧，分支定界节点更少。
        M_mid = self.battery_cfg.soc_max - self.battery_cfg.soc_min  # mid 的值域宽度
        M_thr = (self.battery_cfg.rated_power_kw
                 * self.battery_cfg.time_step_hours
                 * (self.battery_cfg.charge_efficiency + 1.0 / self.battery_cfg.discharge_efficiency)) * 1.05

        expr = 0
        for t in range(n):
            mid = (SOC[t] + SOC[t + 1]) / 2.0
            z = [pulp.LpVariable(f"z_{t}_{k}", cat="Binary") for k in range(K)]
            thr_k = [pulp.LpVariable(f"thr_{t}_{k}", lowBound=0) for k in range(K)]
            prob += pulp.lpSum(z) == 1
            prob += pulp.lpSum(thr_k) == thr[t]
            for k, (lo, hi, coeff) in enumerate(bounds):
                prob += mid >= lo - M_mid * (1 - z[k])
                prob += mid <= hi + M_mid * (1 - z[k])
                prob += thr_k[k] <= M_thr * z[k]
                expr += thr_k[k] * coeff * unit_cost
        return expr

    # ------------------------------------------------------------------ #
    #  能量守恒自检
    # ------------------------------------------------------------------ #
    def _replay_soc(self, charge_power, discharge_power, initial_soc):
        """按充放电功率重放 SOC，返回 (轨迹, 越界步数)。正确实现下越界应为 0。"""
        cap = self.battery_cfg.rated_capacity_kwh
        dt = self.battery_cfg.time_step_hours
        eta_c = self.battery_cfg.charge_efficiency
        eta_d = self.battery_cfg.discharge_efficiency
        n = len(charge_power)
        soc = np.zeros(n + 1)
        soc[0] = initial_soc
        violations = 0
        for t in range(n):
            soc[t + 1] = (soc[t]
                          + charge_power[t] * dt * eta_c / cap
                          - discharge_power[t] * dt / (eta_d * cap))
            if soc[t + 1] > self.battery_cfg.soc_max + 1e-6 or soc[t + 1] < self.battery_cfg.soc_min - 1e-6:
                violations += 1
        return soc, violations

    # ------------------------------------------------------------------ #
    #  基准策略（v1.1 重写：谷段充满 + 尖峰放完，严格能量守恒）
    # ------------------------------------------------------------------ #
    def baseline_strategy(
        self,
        price_profile: np.ndarray,
        ambient_temp_profile: Optional[np.ndarray] = None,
        initial_soc: float = 0.5,
        terminal_soc: Optional[float] = "cyclic",
    ) -> ScheduleResult:
        """
        基准策略：谷段充满、尖峰放完（运维现场最常见的手动策略）

        v1.1 修复：
        - 原实现用"电价75分位"选放电时段，导致只用了 1.05 峰价、完全放过 1.35 尖峰价，
          是个稻草人；改为显式取电价最低档充电、最高档放电。
        - 原实现充放电量算多 10%~15% 后用 np.clip 强行截断，能量账不平；
          改为逐步计算可用余量，SOC 天然不越界。
        - 支持终值 SOC 约束，与优化策略同一口径比较。
        """
        n = self.battery_cfg.num_steps
        dt = self.battery_cfg.time_step_hours
        cap = self.battery_cfg.rated_capacity_kwh
        pmax = self.battery_cfg.rated_power_kw
        eta_c = self.battery_cfg.charge_efficiency
        eta_d = self.battery_cfg.discharge_efficiency
        soc_min, soc_max = self.battery_cfg.soc_min, self.battery_cfg.soc_max

        if ambient_temp_profile is None:
            ambient_temp_profile = np.ones(n) * self.cfg.ambient_temp_c
        if terminal_soc == "cyclic":
            terminal_soc = initial_soc

        charge_power = np.zeros(n)
        discharge_power = np.zeros(n)

        # 电价最低档充电；其余时段按电价从高到低放电
        valley_price = float(np.min(price_profile))
        valley_idx = np.where(price_profile <= valley_price + 1e-9)[0]
        discharge_order = [int(i) for i in np.argsort(-price_profile)
                           if price_profile[i] > valley_price + 1e-9]

        soc = initial_soc

        # ---- 谷段充满 ----
        for t in valley_idx:
            room_kwh = (soc_max - soc) * cap
            if room_kwh <= 1e-9:
                break
            p = min(pmax, room_kwh / (dt * eta_c))
            if p <= 1e-9:
                break
            charge_power[t] = p
            soc += p * dt * eta_c / cap

        # ---- 高价时段放完，保留到终值SOC ----
        floor_soc = max(soc_min, terminal_soc) if terminal_soc is not None else soc_min
        for t in discharge_order:
            avail_kwh = (soc - floor_soc) * cap * eta_d
            if avail_kwh <= 1e-9:
                break
            p = min(pmax, avail_kwh / dt)
            if p <= 1e-9:
                break
            discharge_power[t] = p
            soc -= p * dt / (eta_d * cap)

        soc_values, violations = self._replay_soc(charge_power, discharge_power, initial_soc)

        net_power = discharge_power - charge_power
        charge_energy = float(np.sum(charge_power) * dt)
        discharge_energy = float(np.sum(discharge_power) * dt)
        arbitrage_rev = float(np.sum((discharge_power * price_profile - charge_power * price_profile) * dt))

        thermal_result = self.thermal_model.simulate_full_day(
            net_power, ambient_temp_profile,
            initial_temp=ambient_temp_profile[0], initial_soc=initial_soc)
        deg_result = self.degradation_model.compute_degradation_from_power(
            net_power, initial_soc=initial_soc, time_step_hours=dt)

        net_rev = arbitrage_rev - deg_result.degradation_cost_yuan

        return ScheduleResult(
            charge_power_kw=charge_power,
            discharge_power_kw=discharge_power,
            net_power_kw=net_power,
            soc=soc_values,
            battery_temp_c=thermal_result["temperature_c"],
            arbitrage_revenue_yuan=round(arbitrage_rev, 2),
            degradation_cost_yuan=round(deg_result.degradation_cost_yuan, 2),
            net_revenue_yuan=round(net_rev, 2),
            charge_energy_kwh=round(charge_energy, 2),
            discharge_energy_kwh=round(discharge_energy, 2),
            max_battery_temp_c=round(thermal_result["max_temperature_c"], 2),
            equivalent_cycles=round(deg_result.equivalent_cycles, 4),
            solver_status="Baseline",
            terminal_soc=round(float(soc_values[-1]), 4),
            energy_balance_error_kwh=0.0,
            soc_violation_steps=int(violations),
        )
