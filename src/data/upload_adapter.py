# -*- coding: utf-8 -*-
"""上传数据适配（前后端共享的单一事实来源）。

背景：`adapt_uploaded_data` 与"默认分时电价"曾在两个入口各写一份，于是分叉：

  - 分时电价：两份 `_default_price_by_hour` 都自行硬编码了一组时段划分，与
    `data_generator.PRICE_PERIODS`（前端时段图例用的那份）不一致 —— 24 小时里
    11 小时档位不同、等权日均价差 23%，且该分叉曲线含 6 小时连续同价区间，
    使 MILP 最优解大量退化，实测把求解从 7.2s 拖到 120s（撞满时限）。
  - 标准格式：两份实现都直接 `return raw_df` 原样透传，而下游校验要求的是内部
    字段名 `price_yuan_per_kwh` / `ambient_temp_c`，文档与 UI 却告诉用户用
    `price` / `temp` —— 按文档上传必定报「缺少必要列」。

本模块是这两段逻辑的唯一实现，调用方一律 import；不要在别处再抄一份。
"""
import numpy as np
import pandas as pd

from src.data.data_generator import price_by_hour

# 上传列名别名表：把用户文件里的常见列名（含中英文）归一到内部字段名
UPLOAD_COL_ALIASES = {
    "timestamp": ("timestamp", "time", "datetime", "date_time", "时间", "日期", "时刻"),
    "load_kw": ("load_kw", "load", "power_kw", "负荷", "用电负荷", "负荷功率"),
    "price_yuan_per_kwh": ("price_yuan_per_kwh", "price", "price_cny_per_kwh",
                           "电价", "分时电价", "电费"),
    "ambient_temp_c": ("ambient_temp_c", "ambient_temp", "temperature", "temp",
                       "温度", "环境温度", "气温"),
}

# 各月基准气温（℃），用于无温度列时合成季节曲线
_MONTH_BASE_TEMP = {1: 5, 2: 8, 3: 14, 4: 20, 5: 25, 6: 29,
                    7: 32, 8: 31, 9: 27, 10: 21, 11: 14, 12: 7}


def default_price_by_hour(hour: float) -> float:
    """默认分时电价（元/kWh）——统一委托 PRICE_PERIODS 单一事实来源。"""
    return price_by_hour(hour)


def _synthesize_ambient_temp(timestamp: pd.Series) -> pd.Series:
    """按「月度基准 + 日变化正弦」合成环境温度（℃）。"""
    month, hour = timestamp.dt.month, timestamp.dt.hour
    return (month.map(_MONTH_BASE_TEMP)
            + 3 * np.sin((hour - 6) * np.pi / 12)).round(1)


def adapt_uploaded_data(raw_df: pd.DataFrame) -> pd.DataFrame:
    """把三种支持的上传格式统一为: timestamp, load_kw, price_yuan_per_kwh, ambient_temp_c

    支持：
      1. NREL ComStock 预聚合时间序列（含 out.electricity.total.energy_consumption.kwh）
      2. 中国工业负荷（timestamp + consumption）
      3. 标准格式（timestamp + load_kw，price/temp 可省略，省略时自动补全）
    """
    raw_df = raw_df.rename(columns={str(c).strip(): c for c in raw_df.columns})

    # ---- 1. 中国工业负荷：consumption(Wh/15min) -> load_kw ----
    if "consumption" in raw_df.columns and "load_kw" not in raw_df.columns:
        result = pd.DataFrame()
        result["timestamp"] = (pd.to_datetime(raw_df["timestamp"], utc=True)
                               .dt.tz_convert("Asia/Shanghai").dt.tz_localize(None))
        result["load_kw"] = (raw_df["consumption"] / 1000 * 4).round(1)
        result["price_yuan_per_kwh"] = result["timestamp"].dt.hour.apply(default_price_by_hour)
        result["ambient_temp_c"] = _synthesize_ambient_temp(result["timestamp"])
        return result

    # ---- 2. NREL ComStock：15min kWh -> kW（按 models_used 归一化到单栋建筑）----
    if "out.electricity.total.energy_consumption.kwh" in raw_df.columns:
        result = pd.DataFrame()
        result["timestamp"] = pd.to_datetime(raw_df["timestamp"])
        models = raw_df.get("models_used", pd.Series(1, index=raw_df.index)).replace(0, 1)
        result["load_kw"] = (raw_df["out.electricity.total.energy_consumption.kwh"]
                             / models * 4).round(1)
        result["price_yuan_per_kwh"] = result["timestamp"].dt.hour.apply(default_price_by_hour)
        result["ambient_temp_c"] = _synthesize_ambient_temp(result["timestamp"])
        return result

    # ---- 3. 标准格式：列名归一化 + 按需补全 ----
    _lookup = {str(c).strip().lower(): c for c in raw_df.columns}
    _renames = {}
    for _target, _alias in UPLOAD_COL_ALIASES.items():
        if _target in raw_df.columns:
            continue
        for _a in _alias:
            if _a in _lookup:
                _renames[_lookup[_a]] = _target
                break
    result = raw_df.rename(columns=_renames).copy()
    if "timestamp" not in result.columns:
        return result  # 完全无法识别：交由上层报「缺少必要列」并提示支持的格式

    result["timestamp"] = pd.to_datetime(result["timestamp"], errors="coerce")
    result = result.dropna(subset=["timestamp"]).reset_index(drop=True)

    if "price_yuan_per_kwh" not in result.columns:
        result["price_yuan_per_kwh"] = result["timestamp"].dt.hour.apply(default_price_by_hour)
    if "ambient_temp_c" not in result.columns:
        result["ambient_temp_c"] = _synthesize_ambient_temp(result["timestamp"])
    return result


def synthesized_columns(raw_df: pd.DataFrame) -> list:
    """返回本次上传中被自动补全的列（用于向用户显式披露），中文可读名。"""
    cols = {str(c).strip().lower() for c in raw_df.columns}
    out = []
    if not ({"price", "price_yuan_per_kwh", "price_cny_per_kwh"} & cols):
        out.append("电价（广东工商业分时）")
    if not ({"temp", "temperature", "ambient_temp_c", "ambient_temp"} & cols):
        out.append("环境温度（季节模型）")
    return out
