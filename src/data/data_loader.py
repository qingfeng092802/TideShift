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
