"""
数据生成器
生成符合真实特征的工商业负荷、电价、气象数据
负荷/气温特征参考：NREL商业建筑负荷数据集（合成，非实测）；
电价口径参考：粤发改价格〔2021〕331 号（时段划分见 PRICE_PERIODS，出处见 data/README.md）
"""
import numpy as np
import pandas as pd
from pathlib import Path
from src.utils.config import CONFIG, active_config


def generate_time_index(days: int = 30, start_date: str = "2024-07-01") -> pd.DatetimeIndex:
    """生成15分钟粒度的时间索引"""
    return pd.date_range(start=start_date, periods=days * 96, freq="15min")


def _hour_float(time_idx: pd.DatetimeIndex) -> np.ndarray:
    """
    时间索引 → 小时浮点数。

    pandas ≥3.0 下 `DatetimeIndex.hour` 返回 Index 而非 ndarray，
    与 ndarray 运算时会出问题，这里统一转 float ndarray。
    """
    return np.asarray(time_idx.hour, dtype=float) + np.asarray(time_idx.minute, dtype=float) / 60.0


def generate_ambient_temp(time_idx: pd.DatetimeIndex, base_temp: float = 30.0) -> np.ndarray:
    """
    生成环境温度曲线（夏季典型日）
    日变化规律：凌晨最低，午后最高
    """
    hours = _hour_float(time_idx)
    # 正弦曲线模拟日温度变化，最低在凌晨5点，最高在15点
    daily_variation = 6.0 * np.sin(2 * np.pi * (hours - 9) / 24)
    # 加一点随机波动
    noise = np.random.normal(0, 0.8, len(time_idx))
    return base_temp + daily_variation + noise


def generate_load_profile(
    time_idx: pd.DatetimeIndex,
    ambient_temp: np.ndarray,
    base_load: float = 1500.0,
) -> np.ndarray:
    """
    生成工商业负荷曲线（96点/天）
    特征：
    - 工作日：白天高、夜间低，有明显的早高峰和午间高峰
    - 周末：整体偏低，白天相对平稳
    - 温度修正：夏季高温增加制冷负荷
    - 工艺保底：最低负荷不低于生产基本需求
    """
    n = len(time_idx)
    hours = _hour_float(time_idx)
    weekday = np.asarray(time_idx.weekday)  # 0=周一, 6=周日

    # 基础日负荷模式（工作日）
    # 夜间（0-7点）：低负荷，约40%
    # 早高峰（8-12点）：高负荷，约100%
    # 午间（12-13点）：略降，约85%
    # 下午高峰（13-18点）：高负荷，约105%
    # 晚间（18-22点）：中高负荷，约80%
    # 深夜（22-24点）：低负荷，约50%
    base_pattern = np.ones(n) * 0.4  # 默认夜间

    for i, h in enumerate(hours):
        if 7 <= h < 8:
            base_pattern[i] = 0.4 + 0.6 * (h - 7)  # 爬升
        elif 8 <= h < 12:
            base_pattern[i] = 1.0 + 0.05 * np.sin(np.pi * (h - 8) / 4)
        elif 12 <= h < 13:
            base_pattern[i] = 0.85
        elif 13 <= h < 18:
            base_pattern[i] = 1.05 + 0.03 * np.sin(np.pi * (h - 13) / 5)
        elif 18 <= h < 22:
            base_pattern[i] = 0.80 - 0.30 * (h - 18) / 4
        elif 22 <= h < 24:
            base_pattern[i] = 0.50 - 0.10 * (h - 22) / 2

    # 周末修正：整体降低30%，高峰时段降低更多
    weekend_mask = weekday >= 5
    base_pattern[weekend_mask] *= 0.7

    # 温度-负荷物理修正（夏季制冷）
    # 基于传热学：气温每升高1℃，商业冷负荷增加4-6%，工业制冷增加2-3%
    # 取混合系数3.5%，且仅在温度>26℃时生效
    temp_correction = np.where(
        ambient_temp > 26,
        1.0 + active_config().load.temp_load_coeff_commercial * 0.7 * (ambient_temp - 26),
        1.0
    )

    # 计算负荷
    load = base_load * base_pattern * temp_correction

    # 工艺保底：最低负荷不低于生产基本需求
    load = np.maximum(load, active_config().load.min_process_load_kw)

    # 加随机噪声（±5%）
    noise = np.random.normal(0, 0.03, n)
    load = load * (1 + noise)

    return np.maximum(load, 0)


def generate_price_profile(time_idx: pd.DatetimeIndex) -> np.ndarray:
    """
    生成广东省工商业峰谷分时电价（96 点/天）。

    时段与浮动比例按 **粤发改价格〔2021〕331 号**《关于进一步完善我省峰谷分时电价
    政策有关问题的通知》（2021-08-31）原文设定，出处与换算见 `data/README.md`：

        高峰时段为 10-12 点、14-19 点；低谷时段为 0-8 点；其余为平段。
        尖峰电价执行时间为 7 月、8 月和 9 月三个整月，每天执行时段为
        11-12 时、15-17 时共三个小时，在高峰电价基础上上浮 25%。
        峰 : 平 : 谷 = 1.7 : 1 : 0.38

    此前这里没有月份维度：尖峰全年生效，还把 19:00-21:00 也算进尖峰、低谷只给
    0-7 点——与原文不符，等于用"全年天天尖峰"的日历支撑"广东峰谷电价"这句话。
    """
    hours = _hour_float(time_idx)
    p = active_config().price
    return np.array([price_by_hour(h, month=int(m), price_cfg=p)
                     for h, m in zip(hours, time_idx.month)])


# 单一事实来源：时段划分同时供 generate_price_profile、/api/bootstrap 前端下发与一致性测试使用。
# months=None 表示全年生效；有值表示仅这些月份生效（尖峰）。
# 匹配规则是"窗口最窄者胜"，**不依赖列表顺序**——尖峰窗口是高峰窗口的子集，
# 靠顺序取胜的写法一旦有人调整顺序就会静默改变电价。
PRICE_PERIODS = [
    {"name": "低谷", "cls": "st-ok", "hours": "00:00-08:00",
     "range": [(0, 8)], "price_field": "valley_price", "months": None},
    {"name": "平段", "cls": "st-neutral", "hours": "08:00-10:00, 12:00-14:00, 19:00-24:00",
     "range": [(8, 10), (12, 14), (19, 24)], "price_field": "flat_price", "months": None},
    {"name": "高峰", "cls": "st-warn", "hours": "10:00-12:00, 14:00-19:00",
     "range": [(10, 12), (14, 19)], "price_field": "peak_price", "months": None},
    {"name": "尖峰", "cls": "st-danger", "hours": "11:00-12:00, 15:00-17:00（仅 7/8/9 月）",
     "range": [(11, 12), (15, 17)], "price_field": "spike_price", "months": (7, 8, 9)},
]


def price_by_hour(h: float, *, month: int = None, price_cfg=None) -> float:
    """按单一事实来源 PRICE_PERIODS 返回 h 小时的电价（元/kWh）。

    `month` 决定尖峰是否生效；传 None 表示调用方没有月份上下文，此时**尖峰档不参与
    匹配**（宁可少收不多收），11-12 / 15-17 按高峰计价。

    `month` 与 `price_cfg` 都是关键字参数：原先 `price_cfg` 是第二个位置参数，
    若把 `month` 插在中间，老调用 `price_by_hour(h, cfg)` 会把配置对象当成月份——
    不报错，但静默算错价。
    """
    p = price_cfg or active_config().price
    if not (0.0 <= h < 24.0):
        raise ValueError(f"小时数越界：{h}（应为 0 ≤ h < 24）")
    best, best_span = None, None
    for period in PRICE_PERIODS:
        months = period.get("months")
        if months is not None and (month is None or month not in months):
            continue
        for lo, hi in period["range"]:
            if lo <= h < hi:
                if best_span is None or (hi - lo) < best_span:
                    best, best_span = period, hi - lo
                break
    if best is None:
        # 时段表被改漏了区间。以前这里静默回落到平段价，会把漏洞藏成"看起来正常的电价"。
        raise ValueError(f"电价时段表存在未覆盖的空隙：h={h}, month={month}")
    return getattr(p, best["price_field"])


def generate_dr_signals(time_idx: pd.DatetimeIndex, num_events: int = 2,
                         schedule_date: str = None) -> pd.DataFrame:
    """
    生成需求响应事件信号
    模拟电网在高峰时段下发削峰指令

    Args:
        time_idx: 时间索引
        num_events: 事件数量
        schedule_date: 调度日（格式 'YYYY-MM-DD'）。提供时所有事件在该日生成，
                       避免事件日期与调度日不匹配；不提供时在全部日期中随机选择。
    """
    events = []
    # 在高峰/尖峰时段随机生成 DR 事件。窗口取自 331 号文：高峰 10-12、14-19，
    # 尖峰 11-12、15-17（仅 7/8/9 月）。19、20 点在新口径下是平段，削峰事件落在那里
    # 语义就错了——所以整点集合止于 18。
    peak_hours = [10, 11, 14, 15, 16, 17, 18]
    days = time_idx.normalize().unique()

    if schedule_date is not None:
        target_day = pd.Timestamp(schedule_date).normalize()
        if target_day not in days:
            raise ValueError(f"schedule_date {schedule_date} 不在数据日期范围内")
        selected_days = [target_day] * num_events
    else:
        selected_days = np.random.choice(days, size=min(num_events, len(days)), replace=False)
    for day in selected_days:
        start_hour = np.random.choice(peak_hours)
        duration_hours = np.random.choice([1, 2])
        target_power = np.random.uniform(300, 600)  # 目标削减功率 kW

        events.append({
            "start_time": day + pd.Timedelta(hours=start_hour),
            "end_time": day + pd.Timedelta(hours=start_hour + duration_hours),
            "target_reduction_kw": target_power,
            "subsidy_per_kwh": active_config().price.dr_subsidy_per_kwh,
            "type": "peak_shaving",
        })

    return pd.DataFrame(events)


def generate_all_data(days: int = 30, save_dir: str = "data", seed: int = 42,
                      schedule_date: str = None):
    """生成全部数据并保存为CSV

    Args:
        schedule_date: 调度日（'YYYY-MM-DD'），传入后DR事件固定在该日生成。
    """
    # 行尾显式指定 LF：pandas 默认按 os.linesep 写，Windows 上会造出 CRLF 的 CSV，
    # 与 .gitattributes 钉的 `* text=auto eol=lf` 相反（入库时虽会归一化，但工作树的
    # 字节就和新克隆的不一致，按字节比对/哈希的脚本会误判）。
    np.random.seed(seed)

    time_idx = generate_time_index(days=days)
    ambient_temp = generate_ambient_temp(time_idx)
    load = generate_load_profile(time_idx, ambient_temp)
    price = generate_price_profile(time_idx)
    dr_signals = generate_dr_signals(time_idx, schedule_date=schedule_date)

    # 保存数据
    save_path = Path(save_dir)
    save_path.mkdir(parents=True, exist_ok=True)

    # 负荷+电价+温度合并表
    df = pd.DataFrame({
        "timestamp": time_idx,
        "load_kw": load.round(2),
        # round(5)：尖峰 1.38125 有 5 位小数，取到 4 位会让内置 CSV 与 PriceConfig 不一致
        "price_yuan_per_kwh": price.round(5),
        "ambient_temp_c": ambient_temp.round(2),
        "hour": np.asarray(time_idx.hour),
        "weekday": np.asarray(time_idx.weekday),
        "is_weekend": (np.asarray(time_idx.weekday) >= 5).astype(int),
    })
    df.to_csv(save_path / "load" / "load_data.csv", index=False, encoding="utf-8-sig",
              lineterminator="\n")

    # 电价表（单独保存典型日）
    typical_day = df[df["timestamp"].dt.date == df["timestamp"].dt.date.iloc[0]]
    typical_day[["timestamp", "price_yuan_per_kwh"]].to_csv(
        save_path / "price" / "typical_price.csv", index=False,
        encoding="utf-8-sig", lineterminator="\n"
    )

    # DR信号
    dr_signals.to_csv(save_path / "load" / "dr_signals.csv", index=False,
                    encoding="utf-8-sig", lineterminator="\n")

    # 电池参数
    battery_params = pd.DataFrame([
        {"parameter": "rated_power_kw", "value": active_config().battery.rated_power_kw},
        {"parameter": "rated_capacity_kwh", "value": active_config().battery.rated_capacity_kwh},
        {"parameter": "soc_min", "value": active_config().battery.soc_min},
        {"parameter": "soc_max", "value": active_config().battery.soc_max},
        {"parameter": "charge_efficiency", "value": active_config().battery.charge_efficiency},
        {"parameter": "discharge_efficiency", "value": active_config().battery.discharge_efficiency},
        {"parameter": "thermal_capacity_kj_k", "value": active_config().battery.thermal_capacity_kj_k},
        {"parameter": "thermal_resistance_k_w", "value": active_config().battery.thermal_resistance_k_w},
        {"parameter": "internal_resistance_ohm", "value": active_config().battery.internal_resistance_ohm},
        {"parameter": "temp_normal_max", "value": active_config().battery.temp_normal_max},
        {"parameter": "temp_safe_max", "value": active_config().battery.temp_safe_max},
        {"parameter": "cycle_life", "value": active_config().battery.cycle_life},
        {"parameter": "battery_cost_per_kwh", "value": active_config().battery.battery_cost_per_kwh},
    ])
    battery_params.to_csv(save_path / "battery" / "battery_params.csv", index=False,
                        encoding="utf-8-sig", lineterminator="\n")

    print(f"数据生成完成：")
    print(f"  - 负荷数据：{len(df)} 条 ({days}天 × 96点)")
    print(f"  - 电价数据：{len(typical_day)} 点（典型日）")
    print(f"  - DR事件：{len(dr_signals)} 条")
    print(f"  - 电池参数：{len(battery_params)} 项")
    print(f"  - 保存目录：{save_path.absolute()}")

    return df, dr_signals


if __name__ == "__main__":
    import os
    os.chdir(Path(__file__).parent.parent.parent)
    # DR 事件统一生成在数据集的**最后一天**，避免事件散落在 30 天里、
    # 离线跑某一天时一个事件都命中不到。
    # ⚠️ 需要注意的口径差异：Web 流程**不读取**该 CSV——它的 DR 事件按当前所选
    # 调度日即时生成。而系统默认调度日是可用日期的**中间日**（`server.default_date()`，
    # 30 天数据为 2024-07-15），因此这份 CSV 的日期（2024-07-30）与默认调度日**并不重合**；
    # 它只作为离线示例数据，供 `load_dr_signals()` 与离线实验使用。
    # 若要让二者重合，应改为按 default_date() 的规则取中间日。
    _days, _start = 30, "2024-07-01"
    _schedule_date = (pd.Timestamp(_start) + pd.Timedelta(days=_days - 1)).strftime("%Y-%m-%d")
    generate_all_data(days=_days, schedule_date=_schedule_date)
