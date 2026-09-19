"""数据加载器

默认数据目录不再相对 CWD 解析（CWD 非项目根时 import 期即 FileNotFoundError，
服务起不来）。改为以本文件位置为基准：src/data/data_loader.py → 项目根 = parents[2]。
显式传入 data_dir 参数时行为不变（便于测试注入临时数据目录）。
"""
import pandas as pd
from pathlib import Path

# 项目根目录（src/data/data_loader.py → parents[2] = 项目根）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


def load_load_data(data_dir=None) -> pd.DataFrame:
    """加载负荷+电价+温度数据"""
    base = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    path = base / "load" / "load_data.csv"
    df = pd.read_csv(path, parse_dates=["timestamp"])
    return df


def load_dr_signals(data_dir=None) -> pd.DataFrame:
    """加载需求响应信号"""
    base = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    path = base / "load" / "dr_signals.csv"
    df = pd.read_csv(path, parse_dates=["start_time", "end_time"])
    return df


def load_battery_params(data_dir=None) -> dict:
    """加载电池参数"""
    base = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    path = base / "battery" / "battery_params.csv"
    df = pd.read_csv(path)
    return dict(zip(df["parameter"], df["value"]))


def get_day_data(df: pd.DataFrame, date: str) -> pd.DataFrame:
    """获取指定日期的96点数据"""
    mask = df["timestamp"].dt.date == pd.to_datetime(date).date()
    return df[mask].reset_index(drop=True)


def day_price_temp(df: pd.DataFrame, date: str, num_steps: int = 96):
    """取当日 (电价, 气温) 两条序列；该日不足 num_steps 行时返回 None。

    存在的理由是**可复现**：`data_generator.generate_ambient_temp` 内含一行未播种的
    `np.random.normal(0, 0.8, n)`，谁调用它就现场抽一条新曲线。寻优 Agent 与评测
    harness 此前都直接调它，于是"同一配置重复求解实测能差约 1%"这句写在代码里的话
    其实是错的——差的不是解，是输入（24 点默认约束连跑四次 1352.18 ~ 1392.01 元，
    极差 2.9%，比 2% 的胜出门槛还大；同一组输入重复求解则逐分不差）。
    要评估某一天，就用那一天真正落盘的序列。
    """
    day = get_day_data(df, date)
    if len(day) != num_steps:
        return None
    return (day["price_yuan_per_kwh"].to_numpy(dtype=float),
            day["ambient_temp_c"].to_numpy(dtype=float))
