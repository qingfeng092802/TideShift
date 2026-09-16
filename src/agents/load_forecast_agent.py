"""
负荷预测Agent

功能：预测次日96点（15分钟粒度）工商业用电负荷
算法：XGBoost时序预测 + 传热学物理修正项

能动专业核心壁垒：
1. 温度-负荷物理修正：基于传热学推导，夏季气温每升高1℃，
   商业建筑冷负荷增加4%-6%，工业制冷负荷增加2%-3%
2. 工艺负荷保底：设置最低工艺用能底线，避免预测值低于生产基本需求
"""
from src.utils.logger import get_logger
log = get_logger(__name__)
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional, Tuple
from src.utils.config import CONFIG


@dataclass
class ForecastResult:
    """负荷预测结果"""
    forecast_load_kw: np.ndarray       # 预测负荷 96点 kW
    actual_load_kw: Optional[np.ndarray]  # 实际负荷（用于评估）
    mape: float                        # 平均绝对百分比误差（物理修正后）
    rmse: float                        # 均方根误差
    physical_correction_applied: bool  # 是否应用了物理修正
    correction_magnitude_kw: float     # 物理修正幅度 kW（96 点绝对值之和）
    mape_without_correction: float = 0.0  # 物理修正前的MAPE（用于计算修正提升）
    baseline_mape: float = 0.0         # v1.1：朴素基线MAPE（用于判断模型是否真的有用）
    # 降级说明：非空表示本次预测未走 XGBoost 主路径（历史数据不足等），
    # 上层应把它显示为告警，而不是默默展示一组口径不同的数字。
    fallback_reason: str = ""


# 训练样本下限：滞后特征含 lag_672（7 天），历史不足 8 天时 dropna 后训练集为空。
# 此时若仍强行 fit，XGBoost 会学出荒谬映射，并让残差拟合出的温度斜率爆炸
# （实测物理修正量可达 2.7 万 kW 量级），进而把后续 MILP 推向求解时限。
MIN_TRAIN_ROWS = 96


class NaiveBaselineForecaster:
    """
    朴素基线：过去 N 天「同时刻」负荷均值，区分工作日/周末。

    v1.1 新增。存在的意义只有一个：**给 XGBoost 一个必须跑赢的对手**。
    工商业日负荷的日内周期性极强，这条 3 行的基线往往比复杂模型还准；
    如果一个 XGBoost 跑不赢它，那这个模型就不该上。
    """

    def __init__(self, lookback_days: int = 14, split_weekend: bool = True):
        self.lookback_days = lookback_days
        self.split_weekend = split_weekend

    def predict(self, historical_data: pd.DataFrame, forecast_date: str) -> np.ndarray:
        forecast_dt = pd.to_datetime(forecast_date)
        hist = historical_data[
            historical_data["timestamp"] < forecast_dt.normalize()
        ].copy()
        if len(hist) == 0:
            return np.ones(96) * float(historical_data["load_kw"].mean())

        hist["date"] = hist["timestamp"].dt.normalize()
        recent_days = sorted(hist["date"].unique())[-self.lookback_days:]
        h = hist[hist["date"].isin(recent_days)].copy()

        if self.split_weekend:
            target_weekend = forecast_dt.weekday() >= 5
            h = h[(h["timestamp"].dt.weekday >= 5) == target_weekend]
        if len(h) == 0:
            h = hist[hist["date"].isin(recent_days)].copy()

        h["time_of_day"] = h["timestamp"].dt.strftime("%H:%M")
        mean_by_tod = h.groupby("time_of_day")["load_kw"].mean()

        times = pd.date_range(start=forecast_dt, periods=96, freq="15min")
        global_mean = float(mean_by_tod.mean()) if len(mean_by_tod) else float(hist["load_kw"].mean())
        return np.array([float(mean_by_tod.get(t.strftime("%H:%M"), global_mean)) for t in times])

    def evaluate(self, historical_data: pd.DataFrame, forecast_date: str) -> float:
        """返回该日 MAPE（%）"""
        actual = historical_data[
            historical_data["timestamp"].dt.date == pd.to_datetime(forecast_date).date()
        ]["load_kw"].values[:96]
        if len(actual) < 96:
            return 0.0
        pred = self.predict(historical_data, forecast_date)
        return float(np.mean(np.abs((actual - pred) / actual)) * 100)


class LoadForecastAgent:
    """负荷预测Agent"""

    def __init__(self, config=None, xgb_params=None):
        self.cfg = config or CONFIG
        self.model = None
        self.feature_columns = None
        self.is_trained = False
        # XGBoost 超参数（外部可覆盖，默认值与原硬编码一致）
        self.xgb_params = xgb_params or {}
        # 数据驱动温度修正系数（训练时从验证数据学习）
        self._temp_slope = None       # kW/℃，残差随温度变化的斜率
        self._temp_intercept = None   # kW，截距
        self._temp_correction_fitted = False
        self._max_temp_correction = 80.0  # 单点最大修正幅度(kW)，防止过修正
        self._baseline_mape = 0.0         # v1.1：朴素基线 MAPE（对照用）
        self._insufficient_data = False   # 历史数据不足以支撑滞后特征时置 True（降级朴素基线）
        self.exclude_temp_features = False  # v1.1：ablation 开关，去掉温度特征后物理修正才真正起作用

    def _create_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        构造时序特征

        特征包括：
        - 时间特征：小时、分钟、星期、是否周末、是否节假日
        - 滞后特征：前15min、前1h、前24h、前7天同时刻
        - 滑动窗口特征：过去1h均值、过去24h均值
        - 气象特征：温度、湿度（模拟）
        """
        df = df.copy()
        df["hour"] = df["timestamp"].dt.hour
        df["minute"] = df["timestamp"].dt.minute
        df["hour_float"] = df["hour"] + df["minute"] / 60.0
        df["weekday"] = df["timestamp"].dt.weekday
        df["is_weekend"] = (df["weekday"] >= 5).astype(int)
        df["day_of_year"] = df["timestamp"].dt.dayofyear

        # 周期特征（正弦余弦编码）
        df["hour_sin"] = np.sin(2 * np.pi * df["hour_float"] / 24)
        df["hour_cos"] = np.cos(2 * np.pi * df["hour_float"] / 24)
        df["weekday_sin"] = np.sin(2 * np.pi * df["weekday"] / 7)
        df["weekday_cos"] = np.cos(2 * np.pi * df["weekday"] / 7)

        # 滞后特征
        df["lag_1"] = df["load_kw"].shift(1)    # 前15分钟
        df["lag_4"] = df["load_kw"].shift(4)    # 前1小时
        df["lag_96"] = df["load_kw"].shift(96)  # 前1天同时刻
        df["lag_672"] = df["load_kw"].shift(672)  # 前7天同时刻

        # 滑动窗口特征
        df["rolling_mean_4"] = df["load_kw"].shift(1).rolling(window=4).mean()   # 过去1小时均值
        df["rolling_mean_96"] = df["load_kw"].shift(1).rolling(window=96).mean()  # 过去24小时均值
        df["rolling_std_96"] = df["load_kw"].shift(1).rolling(window=96).std()    # 过去24小时标准差

        # 温度特征
        df["temp"] = df["ambient_temp_c"]
        df["temp_lag_1"] = df["ambient_temp_c"].shift(1)
        df["temp_change"] = df["ambient_temp_c"] - df["ambient_temp_c"].shift(4)

        return df

    def _physical_correction(
        self, forecast_load: np.ndarray, ambient_temp: np.ndarray, mode: str = "data_driven"
    ) -> Tuple[np.ndarray, float]:
        """
        温度修正项

        两种模式：
        - data_driven（默认，用于XGBoost预测）：从训练数据学习温度-残差斜率，
          仅在有显著温度残差关系时修正。XGBoost已含温度特征时斜率≈0，等效不修正。
        - physical（用于predict_simple简化预测）：传热学公式修正，系数已降低。

        参数：
            forecast_load: 预测负荷 96点 kW
            ambient_temp: 环境温度 96点 ℃
            mode: "data_driven" 或 "physical"
        """
        base_temp = 26.0  # 制冷启动温度
        correction = np.zeros_like(forecast_load)

        if mode == "data_driven":
            # 🟠 修复（静默换口径）：此前条件写的是
            #   `if mode == "data_driven" and self._temp_correction_fitted and slope is not None:`
            # 未拟合时直接掉进下面的 else 分支——即"数据驱动"模式悄悄换成了
            # predict_simple 用的物理公式，修正强度差一个量级且调用方无感知。
            # 现在：数据驱动模式未拟合出显著的温度-残差关系 → 明确不修正。
            if not (self._temp_correction_fitted and self._temp_slope is not None):
                return forecast_load, 0.0
            # 数据驱动：用训练时学习的残差斜率修正（绝对kW值，非比例）
            for i in range(len(forecast_load)):
                if ambient_temp[i] > base_temp:
                    correction[i] = self._temp_slope * (ambient_temp[i] - base_temp) + self._temp_intercept
                elif ambient_temp[i] < 18:
                    # 采暖段简化：用斜率的相反数
                    correction[i] = -self._temp_slope * (18 - ambient_temp[i]) * 0.5
            # 限制单点最大修正幅度，防止过修正
            correction = np.clip(correction, -self._max_temp_correction, self._max_temp_correction)
        else:
            # 物理公式模式（predict_simple 用）
            # v1.1 修复：原实现额外乘了 0.05，实际修正强度只有 0.2%/℃，
            # 是 README 声称的 4~6%/℃ 的 1/20，等于把物理修正关掉了。此处恢复真实系数。
            temp_coeff = self.cfg.load.temp_load_coeff_commercial * 0.6 + \
                         self.cfg.load.temp_load_coeff_industrial * 0.4
            for i in range(len(forecast_load)):
                if ambient_temp[i] > base_temp:
                    correction[i] = forecast_load[i] * temp_coeff * (ambient_temp[i] - base_temp)
                elif ambient_temp[i] < 18:
                    correction[i] = forecast_load[i] * 0.02 * (18 - ambient_temp[i])

        corrected_load = forecast_load + correction

        # 工艺保底：不低于最低工艺负荷
        corrected_load = np.maximum(corrected_load, self.cfg.load.min_process_load_kw)

        total_correction = float(np.sum(np.abs(correction)))
        return corrected_load, total_correction

    def _evaluate_naive_baseline(self, historical_data: pd.DataFrame, test_df: pd.DataFrame) -> float:
        """在测试集覆盖的日期上评估朴素基线 MAPE"""
        try:
            dates = sorted(test_df["timestamp"].dt.normalize().unique())
            baseline = NaiveBaselineForecaster()
            scores = []
            for d in dates:
                m = baseline.evaluate(historical_data, d.strftime("%Y-%m-%d"))
                if m > 0:
                    scores.append(m)
            return float(np.mean(scores)) if scores else 0.0
        except Exception:
            return 0.0

    def _fit_data_driven_correction(self, test_df: pd.DataFrame):
        """
        从验证数据学习温度-残差关系，用于数据驱动修正。

        原理：XGBoost已含温度特征，若模型已完全捕获温度效应，
        则残差与温度无显著相关（斜率≈0），修正自动近零。
        若模型未完全捕获（如数据不足），则学习到的斜率可补偿。
        """
        if test_df is None or len(test_df) < 96 or self.model is None:
            self._temp_correction_fitted = False
            return

        X_test = test_df[self.feature_columns]
        y_test = test_df["load_kw"].values
        y_pred = self.model.predict(X_test)
        residuals = y_test - y_pred  # 正=预测偏低，需要正向修正
        temps = test_df["temp"].values

        # 高温段拟合：residual = slope * (temp - 26) + intercept
        hot_mask = temps > 26.0
        if np.sum(hot_mask) >= 10:
            x = temps[hot_mask] - 26.0
            y = residuals[hot_mask]
            slope, intercept = np.polyfit(x, y, 1)
            # 显著性检验：相关系数绝对值 > 0.1 才认为有意义
            corr = np.corrcoef(x, y)[0, 1] if len(x) > 1 else 0.0
            if abs(corr) > 0.1:
                self._temp_slope = float(slope)
                self._temp_intercept = float(intercept)
                self._temp_correction_fitted = True
                log.info(f"[负荷预测Agent] 数据驱动修正拟合: slope={slope:.1f}kW/℃, intercept={intercept:.1f}kW, corr={corr:.3f}")
            else:
                # 无显著温度残差关系，设为近零（XGBoost已捕获温度效应）
                self._temp_slope = 0.0
                self._temp_intercept = 0.0
                self._temp_correction_fitted = True
                log.info(f"[负荷预测Agent] 数据驱动修正: 温度-残差无显著相关(corr={corr:.3f})，修正近零")
        else:
            self._temp_slope = 0.0
            self._temp_intercept = 0.0
            self._temp_correction_fitted = True
            log.info("[负荷预测Agent] 数据驱动修正: 高温段样本不足，修正近零")

    def train(self, historical_data: pd.DataFrame, test_days: int = 3):
        """
        训练XGBoost负荷预测模型

        参数：
            historical_data: 历史负荷数据（含timestamp, load_kw, ambient_temp_c等）
            test_days: 测试集天数
        """
        try:
            from xgboost import XGBRegressor
        except ImportError:
            raise ImportError("请先安装xgboost: pip install xgboost")

        log.info("[负荷预测Agent] 构造特征...")
        df = self._create_features(historical_data)

        # 去掉有NaN的行（滞后特征导致）
        df = df.dropna()

        # 特征列
        self.feature_columns = [
            "hour_float", "weekday", "is_weekend", "day_of_year",
            "hour_sin", "hour_cos", "weekday_sin", "weekday_cos",
            "lag_1", "lag_4", "lag_96", "lag_672",
            "rolling_mean_4", "rolling_mean_96", "rolling_std_96",
            "temp", "temp_lag_1", "temp_change",
        ]
        # v1.1：ablation 开关。XGBoost 吃了温度特征后，"物理修正"就无事可做
        # （残差与温度相关性≈0 → 数据驱动修正自动归零）。
        # 做消融实验时关掉温度特征，物理修正才有独立贡献，对比才有意义。
        if getattr(self, "exclude_temp_features", False):
            self.feature_columns = [c for c in self.feature_columns
                                    if c not in ("temp", "temp_lag_1", "temp_change")]
            log.info("[负荷预测Agent] ablation 模式：已移除温度特征，温度效应由物理修正项承担")

        # 划分训练集和测试集
        test_points = test_days * 96
        train_df = df.iloc[:-test_points] if test_points > 0 else df
        test_df = df.iloc[-test_points:] if test_points > 0 else None

        X_train = train_df[self.feature_columns]
        y_train = train_df["load_kw"]

        log.info(f"[负荷预测Agent] 训练集: {len(X_train)} 条, 测试集: {len(test_df) if test_df is not None else 0} 条")

        # 🟠 数据量下限：滞后特征（lag_672 = 7 天）会把历史不足 8 天的数据整表 dropna，
        #    得到 0 条训练样本。此处显式判定"数据不足"，不 fit，交由 predict() 降级为
        #    朴素基线；同时置 self.model = None，确保任何读取方都不会拿到未训练模型。
        if len(X_train) < MIN_TRAIN_ROWS:
            log.warning("[负荷预测Agent] 历史数据不足：dropna 后仅 %d 条训练样本（需 ≥ %d，"
                        "滞后特征含 7 天前同期值）。本次不训练 XGBoost，改用朴素基线。",
                        len(X_train), MIN_TRAIN_ROWS)
            self.is_trained = False
            self.model = None
            self._insufficient_data = True   # 记住"已判定数据不足"，避免每次预测重复训练
            self._temp_correction_fitted = False
            self._baseline_mape = 0.0
            return
        self._insufficient_data = False

        # 训练XGBoost（默认参数 + 外部覆盖）
        _xgb_kwargs = dict(
            n_estimators=200,
            max_depth=6,
            learning_rate=0.1,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
            verbosity=0,
        )
        _xgb_kwargs.update(self.xgb_params)
        self.model = XGBRegressor(**_xgb_kwargs)
        self.model.fit(X_train, y_train)

        self.is_trained = True
        log.info("[负荷预测Agent] 模型训练完成")

        # 测试集评估
        if test_df is not None and len(test_df) > 0:
            X_test = test_df[self.feature_columns]
            y_test = test_df["load_kw"].values
            y_pred = self.model.predict(X_test)

            mape = float(np.mean(np.abs((y_test - y_pred) / y_test)) * 100)
            rmse = float(np.sqrt(np.mean((y_test - y_pred) ** 2)))
            log.info(f"[负荷预测Agent] 测试集 MAPE: {mape:.2f}%, RMSE: {rmse:.1f} kW")

            # v1.1：与朴素基线对比 —— 跑不赢基线就不该上这个模型
            self._baseline_mape = self._evaluate_naive_baseline(historical_data, test_df)
            log.info(f"[负荷预测Agent] 朴素基线(同时刻均值) MAPE: {self._baseline_mape:.2f}%")
            if self._baseline_mape > 0 and mape > self._baseline_mape:
                log.warning(f"[负荷预测Agent] ⚠ XGBoost 未跑赢朴素基线（差 {mape - self._baseline_mape:+.2f} 个百分点），"
                      f"建议改用直接多步预测或检查特征泄漏")


            # 拟合数据驱动温度修正系数（从验证残差学习）
            self._fit_data_driven_correction(test_df)

            return mape, rmse

        # 无测试集时也尝试拟合（用训练集最后3天）
        if len(df) >= 288:
            self._fit_data_driven_correction(df.iloc[-288:])

        return None, None

    def get_feature_importance(self) -> dict:
        """返回训练好的 XGBoost 模型的特征重要性（归一化到百分比）"""
        if not self.is_trained or self.model is None or self.feature_columns is None:
            return {}
        importances = self.model.feature_importances_
        total = float(np.sum(importances))
        if total <= 0:
            return {}
        result = {}
        for col, imp in zip(self.feature_columns, importances):
            result[col] = float(imp / total * 100)
        # 按重要性降序
        return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))

    def predict(
        self,
        historical_data: pd.DataFrame,
        forecast_date: str,
        apply_physical_correction: bool = True,
    ) -> ForecastResult:
        """
        预测指定日期的96点负荷

        参数：
            historical_data: 历史数据（需包含预测日前的数据）
            forecast_date: 预测日期 "YYYY-MM-DD"
            apply_physical_correction: 是否应用物理修正

        返回：
            ForecastResult
        """
        forecast_dt = pd.to_datetime(forecast_date)
        # 🔴#6 修复（数据泄漏）：此前在过滤预测日**之前**就 self.train(historical_data)，
        # 而 train() 只取最后 3 天做测试集 → 预测日本身就在 XGBoost 训练集内，
        # 随后又对同一天评估 MAPE——对外展示的是训练集内拟合误差，不是泛化能力。
        # 现在先剔除预测日、再训练，训练与评估严格隔离。
        hist = historical_data[
            historical_data["timestamp"].dt.date != forecast_dt.date()
        ].copy()
        hist = hist.sort_values("timestamp").reset_index(drop=True)

        if not self.is_trained and not self._insufficient_data:
            self.train(hist)

        # 🟠 降级路径：历史数据不足以训练 XGBoost（滞后特征需 ≥ 8 天）时，
        #    显式改用朴素基线（过去 14 天同时刻均值，区分工作日/周末），并且
        #    **不做物理修正**——修正量由训练数据拟合，没有可靠训练集时套用
        #    物理公式会得到量级失真的结果（实测曾出现 2.7 万 kW 量级的修正）。
        #    降级原因写入 fallback_reason，由上层显示为告警。
        if not self.is_trained or self.model is None:
            _fc = NaiveBaselineForecaster().predict(historical_data, forecast_date)
            _actual = None
            _day = historical_data[
                historical_data["timestamp"].dt.date == forecast_dt.date()
            ]
            if len(_day) >= 96:
                _actual = _day["load_kw"].values[:96]
            _mape = _rmse = 0.0
            if _actual is not None:
                _mape = float(np.mean(np.abs((_actual - _fc) / _actual)) * 100)
                _rmse = float(np.sqrt(np.mean((_actual - _fc) ** 2)))
            log.warning("[负荷预测Agent] XGBoost 不可用（历史数据不足），"
                        "已降级为朴素基线预测且不做物理修正：%s", forecast_date)
            return ForecastResult(
                forecast_load_kw=_fc,
                actual_load_kw=_actual,
                mape=round(_mape, 2),
                rmse=round(_rmse, 1),
                physical_correction_applied=False,
                correction_magnitude_kw=0.0,
                mape_without_correction=round(_mape, 2),
                baseline_mape=round(_mape, 2),
                fallback_reason=(f"历史数据不足（滞后特征需 ≥ 8 天，当前训练样本不足 "
                                 f"{MIN_TRAIN_ROWS} 条），已降级为朴素基线预测"),
            )

        # 防泄漏断言：预测日必须不在训练集（训练数据里不允许出现预测日任何一点）
        _train_dates = set(pd.to_datetime(hist["timestamp"]).dt.date.unique())
        assert forecast_dt.date() not in _train_dates, \
            f"数据泄漏防护：预测日 {forecast_date} 出现在训练数据中，MAPE 指标将失真"

        forecast_time_idx = pd.date_range(start=forecast_dt, periods=96, freq="15min")

        # 预测日温度：用前一天的温度模式
        prev_day = forecast_dt - pd.Timedelta(days=1)
        prev_day_data = hist[hist["timestamp"].dt.date == prev_day.date()]
        if len(prev_day_data) >= 96:
            ambient_temp = prev_day_data["ambient_temp_c"].values[:96]
        else:
            ambient_temp = np.ones(96) * 30.0

        # 递归预测：逐点预测，每次更新滞后特征
        # 维护一个最近的负荷序列用于计算滞后特征
        recent_loads = hist["load_kw"].values.tolist()
        recent_temps = hist["ambient_temp_c"].values.tolist()
        recent_timestamps = hist["timestamp"].tolist()

        forecast_load = np.zeros(96)

        # 预计算历史同时刻统计（用于递归预测的范围约束，防止误差累积）
        hist["time_of_day"] = hist["timestamp"].dt.strftime("%H:%M")
        tod_stats = hist.groupby("time_of_day")["load_kw"].agg(["mean", "min", "max", "std"]).to_dict("index")

        for i in range(96):
            current_time = forecast_time_idx[i]
            tod_str = current_time.strftime("%H:%M")
            stats = tod_stats.get(tod_str, {"mean": np.mean(recent_loads), "min": np.min(recent_loads), "max": np.max(recent_loads), "std": np.std(recent_loads)})

            # 构造单条特征
            hour_float = current_time.hour + current_time.minute / 60.0
            weekday = current_time.weekday()

            # 滞后特征
            lag_1 = recent_loads[-1] if len(recent_loads) >= 1 else np.mean(recent_loads)
            lag_4 = recent_loads[-4] if len(recent_loads) >= 4 else np.mean(recent_loads)
            lag_96 = recent_loads[-96] if len(recent_loads) >= 96 else np.mean(recent_loads[-96:])
            lag_672 = recent_loads[-672] if len(recent_loads) >= 672 else np.mean(recent_loads)

            # 滑动窗口
            rolling_mean_4 = np.mean(recent_loads[-4:]) if len(recent_loads) >= 4 else np.mean(recent_loads)
            rolling_mean_96 = np.mean(recent_loads[-96:]) if len(recent_loads) >= 96 else np.mean(recent_loads)
            rolling_std_96 = np.std(recent_loads[-96:]) if len(recent_loads) >= 96 else np.std(recent_loads)

            # 温度特征
            temp = ambient_temp[i]
            temp_lag_1 = recent_temps[-1] if len(recent_temps) >= 1 else temp
            temp_change = temp - (recent_temps[-4] if len(recent_temps) >= 4 else temp)

            feature_dict = {
                "hour_float": hour_float,
                "weekday": weekday,
                "is_weekend": int(weekday >= 5),
                "day_of_year": current_time.dayofyear,
                "hour_sin": np.sin(2 * np.pi * hour_float / 24),
                "hour_cos": np.cos(2 * np.pi * hour_float / 24),
                "weekday_sin": np.sin(2 * np.pi * weekday / 7),
                "weekday_cos": np.cos(2 * np.pi * weekday / 7),
                "lag_1": lag_1,
                "lag_4": lag_4,
                "lag_96": lag_96,
                "lag_672": lag_672,
                "rolling_mean_4": rolling_mean_4,
                "rolling_mean_96": rolling_mean_96,
                "rolling_std_96": rolling_std_96,
                "temp": temp,
                "temp_lag_1": temp_lag_1,
                "temp_change": temp_change,
            }

            # 预测
            features = np.array([[feature_dict[col] for col in self.feature_columns]])
            pred = float(self.model.predict(features)[0])

            # 范围约束：防止递归预测误差累积
            # 预测值不超过历史同时刻均值±50%，且不低于历史最小值的80%
            mean_val = stats["mean"]
            min_val = stats["min"]
            max_val = stats["max"]
            lower = max(min_val * 0.8, mean_val * 0.5)
            upper = min(max_val * 1.2, mean_val * 1.5)
            pred = float(np.clip(pred, lower, upper))

            forecast_load[i] = pred

            # 更新历史序列（用于下一个点的滞后特征）
            recent_loads.append(pred)
            recent_temps.append(temp)
            recent_timestamps.append(current_time)

        # 检查是否有实际值用于评估（在物理修正前计算未修正MAPE）
        actual_load = None
        mape_without_correction = 0.0
        forecast_day_data = historical_data[
            historical_data["timestamp"].dt.date == forecast_dt.date()
        ]
        if len(forecast_day_data) >= 96:
            actual_load = forecast_day_data["load_kw"].values[:96]
            mape_without_correction = float(np.mean(np.abs((actual_load - forecast_load) / actual_load)) * 100)

        # 物理修正（数据驱动模式：XGBoost已含温度特征时自动近零修正）
        correction_magnitude = 0
        if apply_physical_correction:
            forecast_load, correction_magnitude = self._physical_correction(
                forecast_load, ambient_temp, mode="data_driven"
            )

        # 修正后的MAPE
        mape = 0
        rmse = 0
        if actual_load is not None:
            mape = float(np.mean(np.abs((actual_load - forecast_load) / actual_load)) * 100)
            rmse = float(np.sqrt(np.mean((actual_load - forecast_load) ** 2)))

        # v1.1：朴素基线 MAPE（同一天、同一口径）
        baseline_mape = 0.0
        if actual_load is not None:
            baseline_mape = NaiveBaselineForecaster().evaluate(historical_data, forecast_date)

        return ForecastResult(
            forecast_load_kw=forecast_load,
            actual_load_kw=actual_load,
            mape=round(mape, 2),
            rmse=round(rmse, 1),
            physical_correction_applied=apply_physical_correction,
            correction_magnitude_kw=round(correction_magnitude, 1),
            mape_without_correction=round(mape_without_correction, 2),
            baseline_mape=round(baseline_mape, 2),
        )

    def predict_simple(
        self,
        historical_data: pd.DataFrame,
        forecast_date: str,
        apply_physical_correction: bool = True,
    ) -> ForecastResult:
        """
        简化版预测：用历史同时刻均值 + 物理修正
        当XGBoost不可用或数据不足时使用
        """
        forecast_dt = pd.to_datetime(forecast_date)

        # 取前7天同时刻的均值
        historical_data = historical_data.copy()
        historical_data["time_of_day"] = historical_data["timestamp"].dt.strftime("%H:%M")

        # 预测日的96个时间点
        forecast_times = pd.date_range(start=forecast_dt, periods=96, freq="15min")
        forecast_load = np.zeros(96)
        ambient_temp = np.zeros(96)

        for i, t in enumerate(forecast_times):
            time_str = t.strftime("%H:%M")
            same_time_data = historical_data[
                historical_data["time_of_day"] == time_str
            ]
            if len(same_time_data) > 0:
                forecast_load[i] = same_time_data["load_kw"].mean()
                ambient_temp[i] = same_time_data["ambient_temp_c"].mean()
            else:
                forecast_load[i] = self.cfg.load.base_load_kw * 0.6
                ambient_temp[i] = 30.0

        # 物理修正（物理公式模式：简化预测无ML温度特征，用传热学公式）
        correction_magnitude = 0
        if apply_physical_correction:
            forecast_load, correction_magnitude = self._physical_correction(
                forecast_load, ambient_temp, mode="physical"
            )

        # 评估
        actual_load = None
        mape = 0
        rmse = 0
        forecast_day_data = historical_data[
            historical_data["timestamp"].dt.date == forecast_dt.date()
        ]
        if len(forecast_day_data) == 96:
            actual_load = forecast_day_data["load_kw"].values
            mape = float(np.mean(np.abs((actual_load - forecast_load) / actual_load)) * 100)
            rmse = float(np.sqrt(np.mean((actual_load - forecast_load) ** 2)))

        return ForecastResult(
            forecast_load_kw=forecast_load,
            actual_load_kw=actual_load,
            mape=round(mape, 2),
            rmse=round(rmse, 1),
            physical_correction_applied=apply_physical_correction,
            correction_magnitude_kw=round(correction_magnitude, 1),
        )
