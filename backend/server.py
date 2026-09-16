"""
FastAPI 后端 - 储能调度系统 Web 版
与原 Streamlit app.py 功能一比一对等：
  6 页面数据 / MILP 求解（缓存+进度）/ 数据上传 / 供应商配置 / 对话 Agent / AI 解释层 / 导出
运行:
    python backend/server.py   # http://127.0.0.1:8800
"""
# 🟠 修复：sys.path 必须在 import src 之前配置。
# 原先这两行在第 23-24 行（import src 之后），导致文档与 start_web.bat 里的
# 启动命令 `python backend/server.py` 必然抛 ModuleNotFoundError: No module named 'src'
# —— Python 只把【脚本所在目录】(backend/) 加进 sys.path，不会加 CWD，
# 所以项目根下的 src 包不可见。此前没暴露，是因为开发时走 pytest（conftest 补了
# sys.path）或 IDE 运行配置；`start_web.bat` 这条"一键启动"路径从未被真正验证过。
import sys
import os
import gc
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 项目根
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                  # backend/（供 import auth）

from src.utils.logger import get_logger
log = get_logger(__name__)
import io
import json
import pickle
import threading
import contextvars
import hashlib
import uuid
import time
import urllib.request as _ureq
from typing import Optional


import numpy as np
import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator, model_validator
from fastapi import Request
import copy
from contextlib import asynccontextmanager

import auth as auth_mod  # P0-02：JWT/Fernet/PBKDF2（backend/auth.py）

from src.data.data_loader import load_load_data
from src.data import upload_adapter as _upload_adapter
from src.data.data_generator import price_by_hour
from src.agents.coordinator_agent import CoordinatorAgent
from src.agents.langgraph_coordinator import LangGraphCoordinator
from src.agents.storage_optimization_agent import StorageOptimizationAgent
from src.agents.demand_response_agent import DRSignal
from src.agents.chat_agent import create_agent, AgentContext, SchedulingTools
from src.utils.config import CONFIG
from src import __version__
from src.utils import cache_security
from src.utils.config import use_config, active_config
from src.utils.logger import get_logger
from src.utils.url_guard import assert_safe_llm_url, SSRFBlockedError
from src.services import model_config_service
from dataclasses import replace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_log = get_logger("server")

# ================= 全局应用状态（对应原 st.session_state） =================
class AppState:
    def __init__(self):
        self.lock = threading.RLock()
        # --- 求解参数（= 原会话默认值） ---
        self.params = {
            "soc_min": 20, "soc_max": 90, "rated_power": 1000,
            "include_thermal": True, "include_degradation": True,
            "use_ml_forecast": True, "enable_dr": True, "orchestrator": "LangGraph",
            "price_peak": 1.35, "price_high": 1.05, "price_flat": 0.65, "price_valley": 0.32,
            "xgb_max_depth": 6, "xgb_lr": 0.1, "xgb_n_est": 200,
            "xgb_temp_corr": True, "xgb_floor": True, "xgb_recursive": True,
        }
        self.custom_data: Optional[pd.DataFrame] = None
        # 🟠 占位值：真实默认日由 default_date() 从当前数据推导后覆盖（见 _get_or_create_session）。
        # 不再预置 "2024-07-15"，避免任何遗漏路径把它当成合法日期使用。
        self.selected_date = ""
        self.manual_dr_signals: list = []
        # --- 求解结果 ---
        self.coordinator = None
        self.report = None
        self.baseline = None
        self.dr_signals: list = []
        self.viz: Optional[dict] = None
        self.engine = "LangGraph"
        self.dr_overlap_warning = ""
        # --- 纯经济策略（电池页对比） ---
        self.pe_cache_key = None
        self.pe_report = None
        self.pe_viz = None
        # --- 求解缓存（内存注册表 + 磁盘 pkl；🟠#19：内存/磁盘均设上限防无界膨胀） ---
        self._SOLVED_KEYS: set = set()
        self._SOLVE_DISK_DIR = os.path.join(ROOT, ".solve_cache")
        self.solve_thread: Optional[threading.Thread] = None
        self.progress = {"running": False, "percent": 0, "label": "", "cache_hit": False,
                         "done": False, "error": "", "warning": ""}
        # --- 模型供应商配置 ---
        self.mp_state = self._load_model_config()
        self.mp_edit_id = self.mp_state.get("active_id")
        # --- 对话 ---
        self.chat_history = [{"role": "assistant",
                              "content": "你好！我是储能调度助手。可以问我收益、温度、调度原因，或让我调参重跑。输入「帮助」查看全部功能。"}]

    # ---------- 数据 ----------
    @property
    def df(self) -> pd.DataFrame:
        return self.custom_data if self.custom_data is not None else self._base_df

    def available_dates(self):
        d = self.df["timestamp"].dt.date.unique()
        return sorted(str(x) for x in d)

    def default_date(self) -> str:
        """默认调度日：一律从当前生效数据的真实日期推导。

        🟠 修复：此前无上传数据时直接 return "2024-07-15"（注释写着"2024-07-08..28
        的 index 7"），与内置 CSV 的真实范围脱钩——一旦内置数据换成别的日期区间
        （部署真实计量数据时的常规动作），该默认日不存在于数据中，切片得到空
        day_data，页面拿不到任何结果（前端表现为白屏）。
        """
        dates = self.available_dates()
        if not dates:
            return ""
        return dates[-1] if len(dates) <= 7 else dates[(len(dates) - 1) // 2]

    # ---------- 供应商配置（🟠#23：与 app.py 统一走 src/services 共享服务层） ----------
    def _model_config_path(self):
        return os.path.join(ROOT, "config", "model_providers.json")

    def _load_model_config(self):
        state = model_config_service.load_model_config(self._model_config_path())
        self._sync_llm_credentials(state)
        return state

    def save_model_config(self):
        # P0-02：API Key 永不明文落盘——共享服务层以 api_key_enc（Fernet）写入
        model_config_service.save_model_config(self._model_config_path(), self.mp_state)
        self._sync_llm_credentials(self.mp_state)

    @staticmethod
    def _sync_llm_credentials(state: dict):
        """把当前激活 provider 的凭据注入解释层默认值。

        🔴 修复：LangGraph 的 explanation_node 以 `LLMExplainer()` 无参构造，只认
        环境变量回退；Web 界面配置的 Key 此前只对对话 Agent 生效，而调度流程内嵌的
        「LLM 决策解释」始终降级规则模板（日志长期 source=rule）。同步后两条链路
        共用同一凭据；对话 Agent 仍走 active_llm()，行为不变。
        """
        from src.agents.llm_explainer import set_default_credentials

        act = state.get("active_id")
        p = next((x for x in state.get("providers", []) if x.get("id") == act),
                 (state.get("providers") or [{}])[0])
        set_default_credentials(
            api_key=p.get("api_key", ""),
            base_url=p.get("base_url", ""),
            model=(p.get("models") or [""])[0],
        )

    def active_provider(self):
        act = self.mp_state.get("active_id")
        for p in self.mp_state.get("providers", []):
            if p["id"] == act:
                return p
        return (self.mp_state.get("providers") or [{"name": "未配置", "base_url": "", "api_key": "", "models": ["deepseek-chat"]}])[0]

    def active_llm(self):
        """(api_key, base_url, model, params)——对话Agent与解释层的唯一取值入口。"""
        p = self.active_provider()
        params = self.mp_state.get("llm_params", {"temperature": 0.3, "max_tokens": 2048, "timeout": 60, "stream": False})
        key = p.get("api_key") or os.getenv("DEEPSEEK_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
        model = p["models"][0] if p.get("models") else "deepseek-chat"
        return key, p.get("base_url", ""), model, params

    def clear_solve_state(self):
        self.coordinator = self.report = self.baseline = None
        self.dr_signals = []
        self.viz = None
        self.pe_cache_key = None
        self.pe_report = self.pe_viz = None


# ================= 会话隔离（🔴#4 修复） =================
# 此前进程级全局单例承载全部会话状态：A 用户上传数据会改掉所有人视图、
# B 能看到 A 的聊天记录、且无法多 worker 部署。
# 现在：按 JWT sub 拆分 AppState，TTL 淘汰 + 数量上限，未认证上下文回退默认会话。
_SESSION_TTL_S = 12 * 3600       # 与 JWT 生命周期一致
_SESSION_MAX = 100               # 会话数量上限（LRU 淘汰最久未用的）

_SESSIONS: dict = {}             # sub -> AppState
_SESSION_STAMPS: dict = {}       # sub -> last_seen timestamp
_SESSIONS_LOCK = threading.Lock()
_CURRENT_SESSION: contextvars.ContextVar = contextvars.ContextVar("current_session", default=None)

# 兼容保留：未登录上下文（exempt 端点/测试直连）使用默认会话
_DEFAULT_SESSION: "AppState" = AppState()
_DEFAULT_SESSION._base_df = load_load_data()
_DEFAULT_SESSION.selected_date = _DEFAULT_SESSION.default_date()


def _sess() -> "AppState":
    """当前请求的会话状态：中间件按 JWT sub 注入；无会话上下文回退默认实例。

    求解工作线程不继承 contextvar——start_solve 启动线程前会显式 set。
    """
    s = _CURRENT_SESSION.get()
    return s if s is not None else _DEFAULT_SESSION


def _get_or_create_session(sub: str) -> "AppState":
    now = time.time()
    with _SESSIONS_LOCK:
        # TTL 淘汰
        for k in [k for k, ts in _SESSION_STAMPS.items() if now - ts > _SESSION_TTL_S]:
            _SESSIONS.pop(k, None)
            _SESSION_STAMPS.pop(k, None)
        # 数量上限：淘汰最久未用
        if len(_SESSIONS) >= _SESSION_MAX:
            oldest = min(_SESSION_STAMPS, key=_SESSION_STAMPS.get)
            _SESSIONS.pop(oldest, None)
            _SESSION_STAMPS.pop(oldest, None)
        if sub not in _SESSIONS:
            s = AppState()
            s._base_df = load_load_data()
            s.selected_date = s.default_date()
            _SESSIONS[sub] = s
        _SESSION_STAMPS[sub] = now
        return _SESSIONS[sub]


def _all_sessions():
    with _SESSIONS_LOCK:
        return list(_SESSIONS.values()) + [_DEFAULT_SESSION]

STAGE_MAP = {
    "init": ("✓ 初始化完成 · 正在负荷预测（XGBoost）", 25),
    "load_forecast": ("✓ 负荷预测完成 · 正在 MILP 优化求解（最耗时，约 1 分钟）", 40),
    "storage_optimization": ("✓ MILP 求解完成 · 正在叠加需求响应", 75),
    "dr_handler": ("✓ 需求响应处理完成 · 正在汇总日报表", 85),
    "finalize": ("✓ 日报表生成完成 · 正在生成决策解释", 92),
    "explanation": ("✓ 决策解释完成", 99),
}

# ================= 数据上传适配（= 原 adapt_uploaded_data） =================
def _default_price_by_hour(h):
    """默认分时电价（元/kWh）——委托 src.data.upload_adapter 单一事实来源。

    🟠 修复：此前本函数自行硬编码时段划分，与 PRICE_PERIODS（前端时段图例所用）
    不一致：24 小时中 11 小时档位不同、日均价差 23%；且该曲线含 6 小时连续同价
    区间使 MILP 最优解大量退化（实测 7.2s → 120s 撞满时限）。已与 app.py 收敛为
    同一实现。
    """
    return _upload_adapter.default_price_by_hour(h)


def adapt_uploaded_data(raw_df: pd.DataFrame) -> pd.DataFrame:
    """把上传文件适配为内部标准格式（实现见 src/data/upload_adapter.py，双前端共用）。

    🟠 修复：此前此处直接 `return raw_df` 原样透传，而下游校验要求内部字段名
    price_yuan_per_kwh / ambient_temp_c，文档与前端却告诉用户用 price / temp，
    按文档上传必然报「缺少必要列」。现统一做列名归一化并按需补全。
    """
    return _upload_adapter.adapt_uploaded_data(raw_df)


# ================= 缓存键与求解（= 原 _current_cache_key/_cached_solve） =================
def _data_fingerprint(_df: pd.DataFrame) -> str:
    """🟠#12 修复：改用 pandas 内容哈希。

    原指纹只含行数+首末时间戳+电价和+温度和——负荷不同但其余相同的数据会命中
    同一缓存键；异常分支用 id()（对象回收后可复用 → 跨数据集缓存碰撞）。
    现在对整表内容做 hash_pandas_object，解析失败直接抛错（拒绝入缓存）。
    """
    content_hash = pd.util.hash_pandas_object(_df, index=False).sum()
    fp = f"{len(_df)}|{content_hash}"
    # P1-B3 修复：MD5→SHA256（截 128 bit）——缓存键不要求密码学强度，
    # 但 MD5 在安全审计/合规检查中必被质疑，且 48 bit 截断空间碰撞概率不可忽视。
    return hashlib.sha256(fp.encode("utf-8")).hexdigest()[:32]


# 电价时段划分规则版本：与 app.py 保持一致；调整时段口径时必须 +1。
PRICE_RULE_VERSION = 2


def current_cache_key() -> str:
    p = _sess().params
    dr = []
    if p["enable_dr"]:
        dr.append(("default",))
    for s in _sess().manual_dr_signals:
        dr.append((str(s.start_time), str(s.end_time), getattr(s, "target_reduction_kw", 0),
                   getattr(s, "subsidy_per_kwh", 0), getattr(s, "dr_type", "")))
    raw = "|".join([
        str(_sess().selected_date),
        # 🟠 电价时段划分规则版本：口径变化必须让旧缓存失效（详见 app.py 同名常量）
        f"pv{PRICE_RULE_VERSION}",
        str(p["orchestrator"]),
        f"{p['soc_min']}", f"{p['soc_max']}", f"{p['rated_power']}",
        ",".join(str(x) for x in (p["price_peak"], p["price_high"], p["price_flat"], p["price_valley"])),
        "1" if p["enable_dr"] else "0", repr(dr),
        "1" if p["use_ml_forecast"] else "0",
        "1" if p["include_thermal"] else "0",
        "1" if p["include_degradation"] else "0",
        f"{p['xgb_max_depth']}", f"{p['xgb_lr']}", f"{p['xgb_n_est']}",
        _data_fingerprint(_sess().df),
    ])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _load_disk(cache_key):
    """P0-01 修复：缓存文件带 HMAC-SHA256 签名，篡改/投毒文件验签失败按未命中处理。
    🟡#35 修复：缓存值从裸 tuple 改为带版本号的 dict（v2），兼容读取旧版 4-tuple。

    🟠 修复（P0）：补上 app.py 已有、此处缺失的 viz 完整性校验。
    旧版 _save_disk 落盘的 viz 恒为 None（解包错位），若不加校验，
    start_solve 会把 s.viz=None 当 cache_hit 返回 → /api/page/* 永远 409、
    前端"多次自动重跑失败"（Web 端白屏）。
    """
    try:
        obj = cache_security.load_signed(_sess()._SOLVE_DISK_DIR, cache_key + ".pkl")
        if isinstance(obj, dict) and obj.get("v") == 2:
            cand = (obj["report"], obj["baseline"], obj["dr"], obj["viz"])
        elif isinstance(obj, tuple) and len(obj) == 4:
            cand = obj  # 旧版格式兼容
        else:
            return None
        _report, _baseline, _dr, _viz = cand
        if not (isinstance(_viz, dict) and _viz.get("time_index") is not None):
            _log.warning("磁盘缓存 viz 缺 time_index（坏条目），按未命中处理 key=%s", cache_key[:8])
            return None
        return cand
    except Exception:
        _log.warning("磁盘缓存读取失败 key=%s", cache_key[:8], exc_info=True)
    return None


def _save_disk(cache_key, obj):
    report, baseline, dr, viz = obj
    cache_security.save_signed(_sess()._SOLVE_DISK_DIR, cache_key + ".pkl",
                               {"v": 2, "report": report, "baseline": baseline,
                                "dr": dr, "viz": viz})
    # 🟠#19：每次写入后执行 mtime LRU 淘汰（上限 50 个文件 / 1GB）
    cache_security.enforce_disk_lru(_sess()._SOLVE_DISK_DIR, max_files=50, max_total_mb=1024.0)


def _cap_solved_keys(s: "AppState", max_keys: int = 64):
    """🟠#19：内存缓存键集合加上限，超限按插入序淘汰（防反复调参撑爆内存）。"""
    if len(s._SOLVED_KEYS) > max_keys:
        for k in sorted(s._SOLVED_KEYS)[:len(s._SOLVED_KEYS) - max_keys]:
            s._SOLVED_KEYS.discard(k)


def cache_available(cache_key) -> bool:
    return (cache_key in _sess()._SOLVED_KEYS) or (_load_disk(cache_key) is not None)


class _CachedCoordinator:
    """磁盘缓存重建的最小协调器（= 原版同款）。"""
    def __init__(self, viz):
        self._viz = viz
    def get_visualization_data(self):
        return self._viz


def _do_solve(cache_key: str):
    """同步执行求解（应在线程中调用）。"""
    p = dict(_sess().params)
    _df = _sess().df.copy()
    _date = _sess().selected_date
    # P0-04：CONFIG frozen 不可变——本线程内注入配置快照，不污染全局，用户间互不干扰
    _solve_cfg = replace(CONFIG, battery=replace(
        CONFIG.battery, soc_min=p["soc_min"] / 100, soc_max=p["soc_max"] / 100,
        rated_power_kw=p["rated_power"]))

    price_custom = {"peak": p["price_peak"], "high": p["price_high"], "flat": p["price_flat"], "valley": p["price_valley"]}
    price_defaults = {"peak": 1.35, "high": 1.05, "flat": 0.65, "valley": 0.32}
    if any(abs(price_custom[k] - price_defaults[k]) > 1e-6 for k in price_defaults):
        # 🟠 时段划分仍走 PRICE_PERIODS（SSOT），只替换价格值——此前这里又内联了一份
        #    时段划分，与内置数据使用的分段不一致。
        from dataclasses import replace as _dc_replace
        _custom_price_cfg = _dc_replace(
            CONFIG.price,
            spike_price=price_custom["peak"], peak_price=price_custom["high"],
            flat_price=price_custom["flat"], valley_price=price_custom["valley"])
        _df = _df.copy()
        _df["price_yuan_per_kwh"] = _df["timestamp"].dt.hour.apply(
            lambda _h: price_by_hour(_h, _custom_price_cfg))

    dr_list = []
    if p["enable_dr"]:
        dr_list.append(("default",))
    for s in _sess().manual_dr_signals:
        dr_list.append((str(s.start_time), str(s.end_time), getattr(s, "target_reduction_kw", 0),
                        getattr(s, "subsidy_per_kwh", 0), getattr(s, "dr_type", "")))
    dr_signals = []
    for d in dr_list:
        if d[0] == "default":
            dr_signals.append(DRSignal(start_time=f"{_date} 15:00", end_time=f"{_date} 17:00",
                                       target_reduction_kw=400.0, subsidy_per_kwh=0.8, dr_type="peak_shaving"))
            dr_signals.append(DRSignal(start_time=f"{_date} 19:30", end_time=f"{_date} 20:30",
                                       target_reduction_kw=500.0, subsidy_per_kwh=1.0, dr_type="peak_shaving"))
        else:
            dr_signals.append(DRSignal(start_time=d[0], end_time=d[1], target_reduction_kw=d[2],
                                       subsidy_per_kwh=d[3], dr_type=d[4]))
    overlap_warning = ""
    if len(dr_signals) > 1:
        sorted_dr = sorted(dr_signals, key=lambda s: str(s.start_time))
        for i in range(len(sorted_dr) - 1):
            if str(sorted_dr[i].end_time) > str(sorted_dr[i + 1].start_time):
                overlap_warning = (f"⚠️ DR事件时间重叠：{sorted_dr[i].start_time}~{sorted_dr[i].end_time} 与 "
                                   f"{sorted_dr[i+1].start_time}~{sorted_dr[i+1].end_time}，可能导致响应量重复计算")
                break

    xgb_params = {"max_depth": int(p["xgb_max_depth"]), "learning_rate": float(p["xgb_lr"]),
                  "n_estimators": int(p["xgb_n_est"])}
    day_data = _df[_df["timestamp"].dt.date == pd.to_datetime(_date).date()]
    price = day_data["price_yuan_per_kwh"].values
    ambient = day_data["ambient_temp_c"].values
    baseline = StorageOptimizationAgent(_solve_cfg).baseline_strategy(price, ambient)

    # use_config 包住求解全程：LangGraph 节点函数内 active_config() 读到本线程快照
    with use_config(_solve_cfg):
        if p["orchestrator"] == "LangGraph":
            coordinator = LangGraphCoordinator(_solve_cfg)
            report = coordinator.run_daily_scheduling(
                date=_date, historical_data=_df, dr_signals=dr_signals,
                use_ml_forecast=p["use_ml_forecast"], include_thermal=p["include_thermal"],
                include_degradation=p["include_degradation"], xgb_params=xgb_params,
                baseline=baseline, progress_cb=lambda node: _on_stage(node))
        else:
            coordinator = CoordinatorAgent(config=_solve_cfg, xgb_params=xgb_params)
            report = coordinator.run_daily_scheduling(
                date=_date, historical_data=_df, dr_signals=dr_signals,
                use_ml_forecast=p["use_ml_forecast"], include_thermal=p["include_thermal"],
                include_degradation=p["include_degradation"])
    viz = coordinator.get_visualization_data()
    _save_disk(cache_key, (report, baseline, dr_signals, viz))
    return coordinator, report, baseline, dr_signals, viz, overlap_warning


def _on_stage(node):
    label, frac = STAGE_MAP.get(node, (node, None))
    if label and frac:
        with _sess().lock:
            _sess().progress["percent"] = frac
            _sess().progress["label"] = label


def start_solve(force_recompute: bool = False):
    """启动后台求解线程；命中缓存则直接同步装载。返回 dict 描述当前动作。"""
    s = _sess()
    with s.lock:
        if s.progress.get("running"):
            return {"action": "already_running"}
        ck = current_cache_key()
        disk = _load_disk(ck)
        if disk is not None and not force_recompute:
            report, baseline, dr, viz = disk
            s.coordinator = _CachedCoordinator(viz)
            s.report, s.baseline, s.dr_signals, s.viz = report, baseline, dr, viz
            s.engine = s.params["orchestrator"]
            s._SOLVED_KEYS.add(ck)
            return {"action": "cache_hit"}
        if force_recompute:
            s._SOLVED_KEYS.discard(ck)
        s.progress.update({"running": True, "percent": 3, "label": "准备调度引擎…",
                           "cache_hit": False, "done": False, "error": "", "warning": ""})

    def _worker():
        # 求解线程不继承 contextvar，显式绑定所属会话（🔴#4）
        _CURRENT_SESSION.set(s)
        t0 = time.time()
        try:
            coordinator, report, baseline, dr, viz, warn = _do_solve(ck)
            with s.lock:
                s.coordinator, s.report, s.baseline = coordinator, report, baseline
                s.dr_signals, s.viz = dr, viz
                s.engine = s.params["orchestrator"]
                s.dr_overlap_warning = warn
                s._SOLVED_KEYS.add(ck)
                _cap_solved_keys(s)
                s.progress.update({"running": False, "percent": 100,
                                   "label": "✅ 全部智能体执行完毕", "done": True, "warning": warn})
            _log.info("求解完成 key=%s 用时=%.1fs", ck[:8], time.time() - t0)
        except Exception as e:
            import traceback
            # 🟠#17：异常落盘日志（带堆栈），不再只截断塞内存
            _log.error("求解失败 key=%s: %s", ck[:8], e, exc_info=True)
            with s.lock:
                s.progress.update({"running": False, "done": True,
                                   "error": f"{type(e).__name__}: {e}",
                                   "trace": traceback.format_exc()[-1500:]})

    t = threading.Thread(target=_worker, daemon=True)
    s.solve_thread = t
    t.start()
    return {"action": "started", "cache_key": ck}


def ensure_solved():
    """页面数据接口守卫：未就绪时自动触发求解。"""
    s = _sess()
    if s.viz is None and not s.progress["running"]:
        start_solve()
    return s.viz is not None


# ================= 序列化工具 =================
def hours_of(viz):
    t = pd.to_datetime(pd.Series(viz["time_index"]))
    return (t.dt.hour + t.dt.minute / 60.0).round(4).tolist()


def report_dict(r) -> dict:
    # 🔴#5：solver_status/time_limit_hit 透传前端——用户永远知道看到的是最优解还是次优解
    return {
        "net_revenue_yuan": float(r.net_revenue_yuan),
        "arbitrage_revenue_yuan": float(r.arbitrage_revenue_yuan),
        "dr_subsidy_yuan": float(getattr(r, "dr_subsidy_yuan", 0.0) or 0.0),
        "degradation_cost_yuan": float(r.degradation_cost_yuan),
        "max_battery_temp_c": float(r.max_battery_temp_c),
        "equivalent_cycles": float(r.equivalent_cycles),
        "forecast_mape": float(getattr(r, "forecast_mape", 0.0) or 0.0),
        "solver_status": str(getattr(r, "solver_status", "")),
        "time_limit_hit": bool(getattr(r, "time_limit_hit", False)),
        "mip_gap_pct": float(getattr(r, "mip_gap_pct", 0.0) or 0.0),
        "soc_violation_steps": int(getattr(r, "soc_violation_steps", 0) or 0),
        "energy_balance_error_kwh": float(getattr(r, "energy_balance_error_kwh", 0.0) or 0.0),
        "explanation": str(getattr(r, "explanation", "") or ""),
        "explanation_source": str(getattr(r, "explanation_source", "none") or "none"),
        # 🟠 此前 alerts 只生成不消费，负荷预测降级等告警在 Web 端完全看不到。
        #    现透传给前端，由总览页以警示条呈现。
        "alerts": [str(a) for a in (getattr(r, "alerts", None) or [])],
    }


def viz_series(viz) -> dict:
    soc = [round(float(s) * 100, 2) for s in viz["soc"][:96]]
    return {
        "time": [str(t) for t in viz["time_index"]][:96],
        "hours": hours_of(viz)[:96],
        "charge_power": [round(float(x), 1) for x in viz["charge_power"]][:96],
        "discharge_power": [round(float(x), 1) for x in viz["discharge_power"]][:96],
        "soc": soc,
        "battery_temp": [round(float(x), 2) for x in viz["battery_temp"]][:96],
        "price": [round(float(x), 3) for x in viz["price"]][:96],
        "load_forecast": [round(float(x), 1) for x in viz["load_forecast"]][:96],
        "load_actual": ([round(float(x), 1) for x in viz["load_actual"]][:96]
                        if viz.get("load_actual") is not None else None),
        "ambient_temp": [round(float(x), 2) for x in viz["ambient_temp"]][:96],
    }


def dr_windows() -> list:
    out = []
    for dr in _sess().dr_signals:
        sh = pd.to_datetime(dr.start_time)
        eh = pd.to_datetime(dr.end_time)
        out.append({"start": sh.hour + sh.minute / 60.0, "end": eh.hour + eh.minute / 60.0,
                    "label": f"{sh.strftime('%H:%M')}-{eh.strftime('%H:%M')}"})
    return out


# ================= FastAPI 应用 =================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # P0-B1：启动登录限流记录清理线程（daemon，随进程退出）
    threading.Thread(target=_login_sweeper_loop, daemon=True, name="login-sweeper").start()
    # P0-B2：启动时扫描 web 静态目录——若混入敏感文件（.json/.pkl/.py/.env 等）给出显式告警
    try:
        _web_dir = os.path.join(ROOT, "web")
        _sensitive = []
        for _root, _dirs, _files in os.walk(_web_dir):
            for _f in _files:
                if os.path.splitext(_f)[1].lower() in (".json", ".pkl", ".py", ".env", ".db", ".sqlite"):
                    _sensitive.append(os.path.relpath(os.path.join(_root, _f), ROOT))
        if _sensitive:
            _log.warning("web/ 静态目录内发现敏感类型文件（可能被静态挂载暴露）：%s", ", ".join(_sensitive))
    except Exception:
        _log.warning("web 目录安全扫描失败", exc_info=True)
    # 🟠#38：优雅关闭——SIGTERM 时等待进行中的求解收尾（此前 daemon 线程直接被砍断）
    yield
    for s in _all_sessions():
        t = s.solve_thread
        if t is not None and t.is_alive():
            _log.info("优雅关闭：等待进行中的求解线程（最长 15s）…")
            t.join(timeout=15)


app = FastAPI(title="储能调度系统 API", lifespan=lifespan)
AUTH = auth_mod.AuthStore()

# P0-02：/api/* 统一 JWT 认证（静态资源与 /api/auth/login、/api/system-info 豁免）
_AUTH_EXEMPT = {"/api/auth/login", "/api/system-info"}
# 🔴#3 修复：must_change=true 期间仅放行改密接口（此前只回传标志不做拦截，默认口令可直调全部 /api/*）
_MUST_CHANGE_ALLOW = {"/api/auth/change-password"}

# 🟠#37：安全响应头（CSP/XFO/nosniff/Referrer-Policy；HTTPS/HSTS 需反代层配置）
# P0-B3 修复：补 HSTS——HTTPS 部署时强制浏览器后续走 TLS，防 SSL 降级中间人
# （纯 HTTP 本地部署时浏览器自动忽略该头，无副作用）。
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    # 前端只加载本站资源；内联样式/脚本为现状所需
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; "
                                "style-src 'self' 'unsafe-inline'; "
                                "script-src 'self' 'unsafe-inline'; "
                                "connect-src 'self'; "
                                "object-src 'none'; base-uri 'self'"),
}

# 🟠#21：登录限流（IP+账号维度，指数退避锁定）——PBKDF2 20万迭代单次约百毫秒 CPU，
# 不限流则既可爆破又构成未认证 CPU 放大 DoS
# P0-B1 修复：两个字典此前无清理机制——攻击者用 1 个 IP + 海量不同 username 打
# /api/auth/login 可把内存灌到 GB 级（OOM DoS）。现在：
#   1) 条目数上限 _LOGIN_MAX_ENTRIES，超出按插入序淘汰最旧项；
#   2) 后台 sweeper 线程每 5 分钟清理已过期（locked_until < now）的锁定记录。
_LOGIN_FAILS: dict = {}          # key -> fail_count
_LOGIN_LOCKED_UNTIL: dict = {}   # key -> lock_until timestamp
_LOGIN_LOCK = threading.Lock()
_LOGIN_MAX_FAILS = 5
_LOGIN_BASE_LOCK_S = 30
_LOGIN_MAX_LOCK_S = 900
_LOGIN_MAX_ENTRIES = 10000       # 防字典无界增长（内存 DoS）


def _login_gate_key(ip: str, username: str) -> str:
    return f"{ip}|{username}"


def _login_rate_limited(key: str) -> Optional[int]:
    """返回剩余锁定秒数；未锁定返回 None。"""
    with _LOGIN_LOCK:
        until = _LOGIN_LOCKED_UNTIL.get(key)
        if until and time.time() < until:
            return int(until - time.time()) + 1
        return None


def _login_record_fail(key: str):
    with _LOGIN_LOCK:
        fails = _LOGIN_FAILS.get(key, 0) + 1
        _LOGIN_FAILS[key] = fails
        if fails >= _LOGIN_MAX_FAILS:
            lock_s = min(_LOGIN_BASE_LOCK_S * (2 ** (fails - _LOGIN_MAX_FAILS)), _LOGIN_MAX_LOCK_S)
            _LOGIN_LOCKED_UNTIL[key] = time.time() + lock_s
            _log.warning("登录失败过多，已锁定 %ss：ip/账号=%s（第 %d 次失败）", lock_s, key, fails)
        # P0-B1：条目数超限时按插入序淘汰最旧项（dict 保留插入序）
        while len(_LOGIN_FAILS) > _LOGIN_MAX_ENTRIES:
            _LOGIN_FAILS.pop(next(iter(_LOGIN_FAILS)))
        while len(_LOGIN_LOCKED_UNTIL) > _LOGIN_MAX_ENTRIES:
            _LOGIN_LOCKED_UNTIL.pop(next(iter(_LOGIN_LOCKED_UNTIL)))


def _login_sweeper_loop():
    """P0-B1：后台线程每 5 分钟清理已过期的锁定/失败计数记录，长期运行防内存缓慢累积。"""
    while True:
        time.sleep(300)
        now = time.time()
        with _LOGIN_LOCK:
            expired = [k for k, u in _LOGIN_LOCKED_UNTIL.items() if u < now]
            for k in expired:
                _LOGIN_LOCKED_UNTIL.pop(k, None)
                _LOGIN_FAILS.pop(k, None)


def _login_record_success(key: str):
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(key, None)
        _LOGIN_LOCKED_UNTIL.pop(key, None)


@app.middleware("http")
async def _jwt_auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in _AUTH_EXEMPT:
        raw = request.headers.get("Authorization") or ""
        token = raw[7:].strip() if raw.startswith("Bearer ") else ""
        payload = auth_mod.jwt_verify(token)
        if payload is None:
            return JSONResponse({"detail": "未登录或会话已过期，请重新登录"}, status_code=401)
        # P0-F2：token 指纹校验——签发时绑定了 UA 指纹的 token，换浏览器环境后失效
        # （旧版无 ua_fp 字段的 token 兼容放行，自然过期淘汰）
        fp = payload.get("ua_fp")
        if fp and fp != auth_mod.ua_fingerprint(request.headers.get("user-agent", "")):
            return JSONResponse({"detail": "登录环境已变化，请重新登录"}, status_code=401)
        sub = payload.get("sub", "")
        request.state.user = sub
        # 🔴#4：按用户注入会话上下文（端点内 _sess() 取到本用户独立状态）
        _CURRENT_SESSION.set(_get_or_create_session(sub))
        # 🔴#3：must_change 强制拦截——未改初始口令前只允许改密接口
        if AUTH.must_change() and path not in _MUST_CHANGE_ALLOW:
            return JSONResponse({"detail": "首次登录请先修改初始口令", "must_change": True},
                                status_code=403)
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


def _engine_config() -> dict:
    """引擎侧真实参数（P1-14/P1-15）：前端热参数与电价时段展示的唯一数据源。"""
    from src.data.data_generator import PRICE_PERIODS
    b = active_config().battery
    pr = active_config().price
    return {
        "thermal": {
            "capacity_kj_k": b.thermal_capacity_kj_k,
            "resistance_k_w": b.thermal_resistance_k_w,
            "internal_resistance_mohm": round(b.internal_resistance_ohm * 1000, 1),
            "efficiency_pct": round(b.charge_efficiency * 100),
            "temp_normal_max": b.temp_normal_max,
            "temp_safe_max": b.temp_safe_max,
        },
        "price_periods": [
            {k: period[k] for k in ("name", "cls", "hours")}
            | {"price": getattr(pr, period["price_field"])}
            for period in PRICE_PERIODS
        ],
    }


@app.get("/api/bootstrap")
def bootstrap():
    p = _sess().params
    # 🟠 修复：日期候选一律来自当前生效数据，不再对内置数据硬编码 2024-07-08~28
    #    （硬编码会把数据里真实存在的 07-01~07-07 / 07-29~07-30 静默隐藏，
    #     且换数据后默认日可能落在范围外 → 空 day_data → 页面白屏）。
    dates = _sess().available_dates()
    key, base, model, _ = _sess().active_llm()
    prov = _sess().active_provider()
    with _sess().lock:
        ck = current_cache_key()
        solved = _sess().viz is not None
        return {
            "params": p,
            "selected_date": _sess().selected_date,
            "dates": dates,
            "data_info": {
                "is_custom": _sess().custom_data is not None,
                "rows": int(len(_sess().df)),
                "days": int(_sess().df["timestamp"].dt.date.nunique()),
            },
            "llm": {"provider": prov.get("name", "未配置"), "model": model,
                    "has_key": bool(key), "base_url": base},
            "solved": solved,
            "cache_available": cache_available(ck),
            "orchestrator": p["orchestrator"],
            "dr_overlap_warning": _sess().dr_overlap_warning,
            # P0-F1：版本号单一事实来源——前端侧栏版本从此字段动态渲染，消除硬编码漂移
            "version": __version__,
            # P1-14/P1-15：引擎参数唯一事实来源，前端展示一律从此读取，禁止硬编码
            "engine_config": _engine_config(),
        }


class SolveReq(BaseModel):
    date: Optional[str] = None
    force: bool = False


@app.post("/api/solve")
def solve(req: SolveReq):
    with _sess().lock:
        if req.date:
            _sess().selected_date = req.date
    return start_solve(force_recompute=req.force)


@app.get("/api/progress")
def get_progress():
    with _sess().lock:
        out = dict(_sess().progress)
        out["solved"] = _sess().viz is not None
        out.pop("trace", None)
        return out


@app.get("/api/page/dashboard")
def page_dashboard():
    if not ensure_solved():
        return JSONResponse({"need_solve": True}, status_code=409)
    rep, base, viz = _sess().report, _sess().baseline, _sess().viz
    b_net = base.arbitrage_revenue_yuan - base.degradation_cost_yuan
    r_net = rep.arbitrage_revenue_yuan - rep.degradation_cost_yuan
    rev_imp = (r_net - b_net) / abs(b_net) * 100 if abs(b_net) > 0.01 else 0
    # 🟡#31：全平电价时 base.arbitrage_revenue_yuan=0 会 ZeroDivisionError → 500（与下方 bv==0 保护不一致）
    arb_imp = ((rep.arbitrage_revenue_yuan - base.arbitrage_revenue_yuan) / base.arbitrage_revenue_yuan * 100
               if abs(base.arbitrage_revenue_yuan) > 1e-9 else 0.0)
    comp_data = [
        ("套利收益", base.arbitrage_revenue_yuan, rep.arbitrage_revenue_yuan),
        ("衰减成本", base.degradation_cost_yuan, rep.degradation_cost_yuan),
        ("净收益", base.net_revenue_yuan, rep.net_revenue_yuan),
        ("最高温度", base.max_battery_temp_c, rep.max_battery_temp_c),
        ("等效循环", base.equivalent_cycles, rep.equivalent_cycles),
    ]
    comp_rows = []
    better_high = {"套利收益", "净收益"}
    for name, bv, ov in comp_data:
        imp = (ov - bv) / abs(bv) * 100 if bv != 0 else 0
        if bv == 0:
            status = "flat"
        elif abs(imp) < 0.05:
            status = "flat"
        else:
            good = (imp > 0) if name in better_high else (imp < 0)
            status = ("good" if good else "bad") if name in better_high else \
                     ("bad" if imp > 0 else "good-cut")
        fmt = (lambda x: f"{x:.3f}") if name == "等效循环" else \
              (lambda x: f"{x:.1f}℃") if name == "最高温度" else (lambda x: f"{x:,.2f}")
        comp_rows.append({"name": name, "base": fmt(bv), "opt": fmt(ov),
                          "imp": round(imp, 1), "status": status})
    annual_arb = rep.arbitrage_revenue_yuan * 365 / 10000
    # 🟡#43：DR 补贴年化系数 50 = 每周有效 DR 事件 ≈ 50 次的显式假设（原为凭空的魔法数）
    annual_dr = rep.dr_subsidy_yuan * 50 / 10000
    annual_deg = rep.degradation_cost_yuan * 365 / 10000
    return {
        "kpis": {
            "net": {"value": rep.net_revenue_yuan, "delta": round(rev_imp, 1)},
            "arb": {"value": rep.arbitrage_revenue_yuan, "delta": round(arb_imp, 1)},
            "temp": {"value": rep.max_battery_temp_c, "warn": rep.max_battery_temp_c >= 45},
            "cycles": {"value": rep.equivalent_cycles, "deg": rep.degradation_cost_yuan},
        },
        "waterfall": {
            "x": ["峰谷套利", "DR补贴", "衰减成本", "净收益"],
            "y": [rep.arbitrage_revenue_yuan, rep.dr_subsidy_yuan, -rep.degradation_cost_yuan],
            "total": rep.net_revenue_yuan,
        },
        "comp_rows": comp_rows,
        "annual": {"arb": round(annual_arb, 1), "dr": round(annual_dr, 1),
                   "deg": round(annual_deg, 1), "net": round(annual_arb + annual_dr - annual_deg, 1)},
        # 🔴fix30：此前本端点未返回 alerts，而 web/js/pages.js 的告警警示条读的正是
        #   d.alerts —— 字段缺失导致该渲染分支恒为假，负荷预测降级 / 温度超限
        #   等告警在「数据总览」页永远不显示（渲染代码成了死代码）。
        #   现与 /api/page/thermal 的 report_dict() 保持同构透传。
        "alerts": [str(a) for a in (getattr(rep, "alerts", None) or [])],
        "series": viz_series(viz),
        "dr_windows": dr_windows(),
        "soc_range": [_sess().params["soc_min"], _sess().params["soc_max"]],
        "explanation": {"text": getattr(rep, "explanation", "") or "",
                        "source": getattr(rep, "explanation_source", "none") or "none"},
    }


@app.get("/api/page/scheduling")
def page_scheduling():
    if not ensure_solved():
        return JSONResponse({"need_solve": True}, status_code=409)
    rep, base, viz = _sess().report, _sess().baseline, _sess().viz
    p = _sess().params
    charge_total = float(np.sum(viz["charge_power"])) * 0.25
    discharge_total = float(np.sum(viz["discharge_power"])) * 0.25
    soc_start = float(viz["soc"][0])
    soc_end = float(viz["soc"][-1])
    # 🟡#43：SOC 能量折算取引擎实际配置的额定容量（原硬编码 2000kWh，改参数后失真）
    _rated_cap = active_config().battery.rated_capacity_kwh
    delta_soc_energy = (soc_start - soc_end) * _rated_cap
    effective_charge = charge_total + max(0, delta_soc_energy)
    rt_eff = min(discharge_total / effective_charge * 100 if effective_charge > 0 else 0, 100.0)
    # 🟡#31：零除保护（全平电价时 base 套利收益为 0）
    arb_imp = ((rep.arbitrage_revenue_yuan - base.arbitrage_revenue_yuan) / base.arbitrage_revenue_yuan * 100
               if abs(base.arbitrage_revenue_yuan) > 1e-9 else 0.0)
    prices = [
        {"tag": "尖峰", "range": "10:00-12:00", "price": p["price_peak"], "tone": "danger"},
        {"tag": "高峰", "range": "08-10, 18-21", "price": p["price_high"], "tone": "warning"},
        {"tag": "平段", "range": "07-08, 12-18", "price": p["price_flat"], "tone": "flat"},
        {"tag": "低谷", "range": "00-07, 21-24", "price": p["price_valley"], "tone": "ok"},
    ]
    return {
        "price_cards": prices,
        "kpis": {"arb": rep.arbitrage_revenue_yuan, "charge": charge_total,
                 "discharge": discharge_total, "rt_eff": rt_eff,
                 "rated_power": p["rated_power"],
                 "peak_discharge": float(np.max(viz["discharge_power"])),
                 "released": max(0, delta_soc_energy),
                 "effective_charge": effective_charge},
        "series": viz_series(viz),
        "soc_range": [p["soc_min"], p["soc_max"]],
        "compare": {"opt": report_dict(rep), "base": report_dict(base),
                    "arb_imp": round(arb_imp, 1)},
    }


def _compute_pure_econ():
    """🟠#20：thermal 页的纯经济口径需要跑第二次完整 MILP（同步，最长 120s）。
    加 single-flight 锁：并发请求共享同一次计算（后到者等锁后直接命中缓存），
    且参数/结果读写全部持锁，消除写竞争。完整异步化（后台任务+轮询）见报告阶段3建议。
    P1-#20 收尾：结果落磁盘缓存（v2 dict 格式，复用 cache_security 签名通道）——
    服务重启后同参数免重算 120s MILP；缓存键含数据指纹，换数据不碰撞。"""
    p = _sess().params
    fp8 = _data_fingerprint(_sess().df)[:8]
    ck = (f"pe_{fp8}_{_sess().selected_date}_{p['soc_min']}_{p['soc_max']}"
          f"_{p['rated_power']}_{p['use_ml_forecast']}")
    with _PE_SINGLE_FLIGHT_LOCK:
        if _sess().pe_cache_key == ck and _sess().pe_report is not None:
            return
        # 磁盘缓存优先（内存未命中：首次访问或服务重启后）
        try:
            disk_obj = cache_security.load_signed(_sess()._SOLVE_DISK_DIR, ck + ".pkl")
            if isinstance(disk_obj, dict) and disk_obj.get("v") == 2:
                _sess().pe_report = disk_obj["pe_report"]
                _sess().pe_viz = disk_obj["pe_viz"]
                _sess().pe_cache_key = ck
                log.info("thermal pe 磁盘缓存命中: %s", ck)
                return
        except Exception:
            log.warning("thermal pe 磁盘缓存读取失败，走重算", exc_info=True)
        _pe_cfg = replace(CONFIG, battery=replace(
            CONFIG.battery, soc_min=p["soc_min"] / 100, soc_max=p["soc_max"] / 100,
            rated_power_kw=p["rated_power"]))
        with use_config(_pe_cfg):
            coord = LangGraphCoordinator(_pe_cfg) if p["orchestrator"] == "LangGraph" else CoordinatorAgent(config=_pe_cfg)
            pe_report = coord.run_daily_scheduling(
                date=_sess().selected_date, historical_data=_sess().df, dr_signals=_sess().dr_signals,
                use_ml_forecast=p["use_ml_forecast"], include_thermal=False, include_degradation=False)
        _sess().pe_report = pe_report
        _sess().pe_viz = coord.get_visualization_data()
        _sess().pe_cache_key = ck
        # 落盘（失败不致命：下次重算即可）
        try:
            cache_security.save_signed(_sess()._SOLVE_DISK_DIR, ck + ".pkl",
                                       {"v": 2, "pe_report": pe_report, "pe_viz": _sess().pe_viz})
        except Exception:
            log.warning("thermal pe 磁盘缓存写入失败", exc_info=True)


_PE_SINGLE_FLIGHT_LOCK = threading.Lock()


@app.get("/api/page/thermal")
def page_thermal():
    if not ensure_solved():
        return JSONResponse({"need_solve": True}, status_code=409)
    _compute_pure_econ()
    rep, viz = _sess().report, _sess().viz
    # 🟡#43：58 = 96 点中的 14:30 时刻（58/4=14.5h），原为无注释魔法数
    cur_idx = min(58, len(viz["soc"]) - 1)
    from src.models.battery_thermal_model import estimate_temperature_rise
    reject_temp = estimate_temperature_rise(power_kw=800, duration_hours=1.0,
                                            ambient_temp=float(np.mean(viz["ambient_temp"])),
                                            initial_temp=float(rep.max_battery_temp_c))
    # DR 事件日志
    dr_responses = viz.get("dr_responses", [])
    log_rows = []
    for i, dr in enumerate(dr_responses):
        sig = _sess().dr_signals[i] if i < len(_sess().dr_signals) else None
        time_str = pd.to_datetime(sig.start_time).strftime("%H:%M") if sig else "-"
        event_type = "削峰响应（电网指令）" if sig and sig.dr_type == "peak_shaving" else "填谷响应"
        target_kw = sig.target_reduction_kw if sig else 0
        if dr.accepted:
            log_rows.append({"time": time_str, "type": event_type,
                             "action": f"要求放电{target_kw:.0f}kW → 实际{dr.response_power_kw:.0f}kW",
                             "thermal": f"✅ 通过（温升{dr.max_temp_during_dr_c - np.mean(viz['ambient_temp']):.1f}℃）",
                             "status": "执行", "net": f"+{dr.net_dr_revenue_yuan:.0f}元"})
        else:
            log_rows.append({"time": time_str, "type": event_type,
                             "action": f"要求放电{target_kw:.0f}kW",
                             "thermal": f"❌ 驳回（预计超温{dr.max_temp_during_dr_c:.1f}℃）",
                             "status": "拒绝", "net": "0元"})
    # 🟠#13 修复：删除硬编码虚构日志"15:45 削峰响应 800kW 驳回"——默认 DR 事件中
    # 并不存在该事件，此前无条件混进生产返回值且前端无标注，属于展示编造内容。
    # 热安全"预驳回"评估保留为独立字段，前端可按需展示。
    return {
        "eng": {"report": report_dict(rep), "series": viz_series(viz)},
        "pure_econ": {"report": report_dict(_sess().pe_report), "series": viz_series(_sess().pe_viz)},
        "current": {"soc": float(viz["soc"][cur_idx] * 100), "temp": float(viz["battery_temp"][cur_idx]),
                    "label": "14:30 时刻"},
        "soc_range": [_sess().params["soc_min"], _sess().params["soc_max"]],
        "ambient_mean": round(float(np.mean(viz["ambient_temp"])), 1),
        "dr_log": log_rows,
        # 热安全预评估（真实计算值，非事件日志）：800kW 持续 1h 的预估温度
        "thermal_preflight": {"power_kw": 800, "duration_hours": 1.0,
                              "estimated_temp_c": round(reject_temp, 1),
                              "threshold_c": active_config().battery.temp_normal_max},
    }


@app.get("/api/page/dr")
def page_dr():
    if not ensure_solved():
        return JSONResponse({"need_solve": True}, status_code=409)
    viz = _sess().viz
    dr_responses = viz.get("dr_responses", [])
    hist = []
    for i, d in enumerate(dr_responses):
        sig = _sess().dr_signals[i] if i < len(_sess().dr_signals) else None
        t = (f"{pd.to_datetime(sig.start_time).strftime('%H:%M')}-{pd.to_datetime(sig.end_time).strftime('%H:%M')}"
             if sig else "-")
        dtype = "削峰" if sig and sig.dr_type == "peak_shaving" else "填谷" if sig and sig.dr_type == "valley_filling" else "备用"
        hist.append({"time": t, "type": dtype,
                     "target": f"{sig.target_reduction_kw:.0f}" if sig else "-",
                     "energy": f"{d.response_energy_kwh:.0f}",
                     "subsidy": f"{d.subsidy_revenue_yuan:.2f}",
                     "accepted": bool(d.accepted),
                     "reason": (d.reason if not d.accepted else "-") or "-"})
    total_subsidy = sum(d.subsidy_revenue_yuan for d in dr_responses if d.accepted)
    total_energy = sum(d.response_energy_kwh for d in dr_responses if d.accepted)
    accepted = sum(1 for d in dr_responses if d.accepted)
    gantt, details = [], []
    for i, d in enumerate(dr_responses):
        sig = _sess().dr_signals[i] if i < len(_sess().dr_signals) else None
        if not sig:
            continue
        sh = pd.to_datetime(sig.start_time)
        eh = pd.to_datetime(sig.end_time)
        details.append({"idx": i + 1, "accepted": bool(d.accepted),
                        "time": f"{sh.strftime('%H:%M')}-{eh.strftime('%H:%M')}",
                        "target": sig.target_reduction_kw, "actual": d.response_power_kw,
                        "subsidy": d.subsidy_revenue_yuan, "net": d.net_dr_revenue_yuan})
    max_temp_dr = max((d.max_temp_during_dr_c for d in dr_responses), default=0)
    temp_pass = max_temp_dr < 45
    return {
        "history": hist,
        "kpis": {"total": len(dr_responses), "subsidy": round(total_subsidy, 2),
                 "energy": round(total_energy), "rejected": len(dr_responses) - accepted},
        "gantt": gantt or None,
        "details": details,
        "feasibility": {"temp_pass": bool(temp_pass), "max_temp": round(max_temp_dr, 1),
                        "rated_power": _sess().params["rated_power"]},
    }


@app.get("/api/page/forecast")
def page_forecast():
    if not ensure_solved():
        return JSONResponse({"need_solve": True}, status_code=409)
    rep, viz = _sess().report, _sess().viz
    load_fc = np.asarray(viz["load_forecast"], dtype=float)
    load_act = viz.get("load_actual")
    load_act = np.asarray(load_act, dtype=float) if load_act is not None else load_fc
    mape_raw = float(viz.get("forecast_mape_raw", 0.0) or 0.0)
    mape_cur = float(rep.forecast_mape or 0.0)
    phys_imp = (mape_raw - mape_cur) / mape_raw * 100 if mape_raw > 0 else 0.0
    # 特征重要性 top6
    fi_raw = viz.get("feature_importance", {}) or {}
    fi_label = {"lag_1": "滞后15min负荷", "lag_4": "滞后1h负荷", "lag_96": "滞后1天负荷",
                "lag_672": "滞后7天负荷", "rolling_mean_4": "近1h均值", "rolling_mean_96": "近24h均值",
                "rolling_std_96": "近24h波动", "hour_float": "小时", "weekday": "星期",
                "is_weekend": "是否周末", "day_of_year": "年内日序", "hour_sin": "小时(周期)",
                "hour_cos": "小时(周期)", "weekday_sin": "星期(周期)", "weekday_cos": "星期(周期)",
                "temp": "环境温度", "temp_lag_1": "温度(滞后)", "temp_change": "温度变化"}
    fi = [{"name": fi_label.get(k, k), "value": round(float(v), 1)}
          for k, v in list(fi_raw.items())[:6]]
    # 误差直方图（20 bins, clip 0-20）
    synthetic = False
    if load_act is not None and not np.array_equal(load_act, load_fc) and np.all(load_act != 0):
        errors = np.clip(np.abs(load_fc - load_act) / load_act * 100, 0, 20)
    else:
        # 🟠#13 修复：无实际负荷时不再编造"预测误差直方图"冒充真实数据——
        # 改为返回空直方图并显式标注 synthetic=true，前端打"示例数据"角标
        synthetic = True
        errors = np.zeros(96)
    counts, edges = np.histogram(errors, bins=20, range=(0, 20))
    hist = [{"mid": round((edges[i] + edges[i + 1]) / 2, 2), "count": int(counts[i])} for i in range(20)]
    # 工作日/周末平均形态
    _df = _sess().df.copy()
    _df["_hi"] = _df["timestamp"].dt.hour * 4 + _df["timestamp"].dt.minute // 15
    _df["_wk"] = _df["timestamp"].dt.weekday >= 5
    wd = _df[~_df["_wk"]].groupby("_hi")["load_kw"].mean()
    we = _df[_df["_wk"]].groupby("_hi")["load_kw"].mean()
    h96 = [round(i / 4, 3) for i in range(96)]
    wd_vals = [round(float(wd.get(i)), 1) if pd.notna(wd.get(i)) else None for i in range(96)]
    we_vals = [round(float(we.get(i)), 1) if pd.notna(we.get(i)) else None for i in range(96)]
    return {
        "kpis": {"mape": mape_cur, "peak": float(np.max(load_fc)), "valley": float(np.min(load_fc)),
                 "phys_imp": round(phys_imp, 1), "mape_raw": mape_raw,
                 "is_custom": _sess().custom_data is not None},
        "series": viz_series(viz),
        "has_actual": load_act is not None and not np.array_equal(load_act, load_fc),
        "feature_importance": fi,
        "error_hist": hist,
        "error_hist_synthetic": synthetic,  # 🟠#13：true=无实际数据，直方图为空（前端显示"暂无数据"）
        "weekday_profile": {"hours": h96, "weekday": wd_vals, "weekend": we_vals},
    }


# ---------------- 上传 ----------------
_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024  # 50MB


@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    try:
        # P1-05 输入校验：50MB 上限 / 最少 96 点 / 时间连续 / 数值范围
        # 🔴#9 修复：此前先 await file.read() 全量读入内存再判 50MB——Starlette 超 1MB
        # 先落盘 temp，任意大 body 可同时打爆内存与磁盘。现在：
        #   1) Content-Length 预判直接拒绝；2) 分块流式读取，累计超限即断。
        _cl = request.headers.get("content-length")
        if _cl and _cl.isdigit() and int(_cl) > _UPLOAD_LIMIT_BYTES:
            return JSONResponse({"ok": False, "msg": "文件超过 50MB 上限"}, status_code=400)
        # P1-B1 修复：分块写入临时文件 + pd.read_csv(path) 流式解析——
        # 此前 b"".join(chunks) 把 50MB 全量拼进内存（峰值 ~100MB），低配设备并发上传可 OOM。
        # 现在内存峰值只有单块 1MB 缓冲区，与文件大小无关；退出时无条件清理临时文件。
        _tmp_path = None
        _total = 0
        try:
            with tempfile.NamedTemporaryFile(prefix="up_", suffix=".csv", delete=False) as _tf:
                _tmp_path = _tf.name
                while True:
                    _chunk = await file.read(1024 * 1024)  # 1MB 分块
                    if not _chunk:
                        break
                    _total = _total + len(_chunk)
                    if _total > _UPLOAD_LIMIT_BYTES:
                        return JSONResponse({"ok": False, "msg": "文件超过 50MB 上限"}, status_code=400)
                    _tf.write(_chunk)
            raw = pd.read_csv(_tmp_path)
        finally:
            if _tmp_path:
                try:
                    os.unlink(_tmp_path)
                except OSError:
                    pass
        udf = adapt_uploaded_data(raw)
        req_cols = ["timestamp", "load_kw", "price_yuan_per_kwh", "ambient_temp_c"]
        if not all(c in udf.columns for c in req_cols):
            return JSONResponse({"ok": False, "msg": "缺少必要列: " + ", ".join(req_cols)}, status_code=400)
        udf["timestamp"] = pd.to_datetime(udf["timestamp"], errors="coerce")
        _bad_ts = int(udf["timestamp"].isna().sum())
        if _bad_ts:
            return JSONResponse({"ok": False, "msg": f"存在 {_bad_ts} 条无法解析的时间戳"}, status_code=400)
        if not udf["timestamp"].is_monotonic_increasing:
            return JSONResponse({"ok": False, "msg": "时间戳未按升序排列（要求时间连续有序）"}, status_code=400)
        if len(udf) < 96:
            return JSONResponse({"ok": False, "msg": f"数据仅 {len(udf)} 条，至少需要 96 条（一天 15min 粒度）"}, status_code=400)
        for _col, _lo, _hi, _name in [("load_kw", 0, 5e5, "负荷"),
                                      ("price_yuan_per_kwh", 0, 20, "电价"),
                                      ("ambient_temp_c", -60, 90, "温度")]:
            _s = pd.to_numeric(udf[_col], errors="coerce")
            if _s.isna().any() or not np.isfinite(_s.fillna(0)).all():
                return JSONResponse({"ok": False, "msg": f"{_name}列存在非数值数据"}, status_code=400)
            if (_s < _lo).any() or (_s > _hi).any():
                return JSONResponse({"ok": False, "msg": f"{_name}列超出合理范围 [{_lo:g}, {_hi:g}]"}, status_code=400)
        btype = str(raw.get("in.comstock_building_type", pd.Series(["标准格式"])).iloc[0])
        with _sess().lock:
            _sess().custom_data = udf
            _sess().clear_solve_state()
            _sess().selected_date = _sess().default_date()
        dates = _sess().available_dates()
        return {"ok": True, "msg": f"已加载 {len(udf)} 条数据 · {btype} · {len(dates)}天 · 请点击运行调度",
                "rows": int(len(udf)), "days": len(dates), "dates": dates,
                "default_date": _sess().selected_date}
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"ok": False, "msg": f"解析失败: {e}"}, status_code=400)


@app.delete("/api/upload")
def clear_upload():
    with _sess().lock:
        _sess().custom_data = None
        _sess().clear_solve_state()
        _sess().selected_date = _sess().default_date()
    # P1-B5：显式触发垃圾回收——pandas 大 DataFrame 引用计数归零后可能不立即归还 OS，
    # 长期运行的"上传-清除"循环会内存碎片化/缓慢增长
    gc.collect()
    return {"ok": True}


# ---------------- 导出 ----------------
@app.get("/api/export/schedule")
def export_schedule():
    if not ensure_solved():
        raise HTTPException(409, "not solved")
    viz = _sess().viz
    res = pd.DataFrame({
        "时间": [str(t) for t in viz["time_index"]][:96],
        "充电功率(kW)": viz["charge_power"][:96],
        "放电功率(kW)": viz["discharge_power"][:96],
        "SOC(%)": [round(s * 100, 2) for s in viz["soc"][:96]],
        "电池温度(℃)": viz["battery_temp"][:96],
        "电价(元/kWh)": viz["price"][:96],
        "预测负荷(kW)": viz["load_forecast"][:96],
    })
    buf = io.StringIO()
    res.to_csv(buf, index=False, encoding="utf-8-sig")
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=schedule_{_sess().selected_date}.csv"})


@app.get("/api/export/plan")
def export_plan():
    if not ensure_solved():
        raise HTTPException(409, "not solved")
    viz = _sess().viz
    buf = io.StringIO()
    buf.write("时间,充电功率(kW),放电功率(kW),SOC(%),电池温度(℃),电价(元/kWh)\n")
    for i in range(96):
        t = str(viz["time_index"][i]) if i < len(viz["time_index"]) else f"{i//4:02d}:{(i%4)*15:02d}"
        ch = viz["charge_power"][i] if i < len(viz["charge_power"]) else 0
        dis = viz["discharge_power"][i] if i < len(viz["discharge_power"]) else 0
        soc = viz["soc"][i] * 100 if i < len(viz["soc"]) else 0
        temp = viz["battery_temp"][i] if i < len(viz["battery_temp"]) else 25
        price = viz["price"][i] if i < len(viz["price"]) else 0
        buf.write(f"{t},{ch:.1f},{dis:.1f},{soc:.1f},{temp:.1f},{price:.2f}\n")
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=energy_storage_schedule_plan.csv"})


# ---------------- 参数 / 设置 ----------------
class ParamsReq(BaseModel):
    # P1-06：Pydantic 字段校验（越界/非法组合在入口即拒，返回 422）
    @field_validator("soc_min")
    @classmethod
    def _v_soc_min(cls, v):
        if v is not None and not (5 <= v <= 60):
            raise ValueError("最低SOC应在 5-60 之间")
        return v

    @field_validator("soc_max")
    @classmethod
    def _v_soc_max(cls, v):
        if v is not None and not (40 <= v <= 100):
            raise ValueError("最高SOC应在 40-100 之间")
        return v

    @field_validator("rated_power")
    @classmethod
    def _v_power(cls, v):
        if v is not None and not (50 <= v <= 10000):
            raise ValueError("额定功率应在 50-10000 kW 之间")
        return v

    @field_validator("price_peak", "price_high", "price_flat", "price_valley")
    @classmethod
    def _v_price(cls, v):
        if v is not None and not (0 < v <= 20):
            raise ValueError("电价应在 (0, 20] 元/kWh 之间")
        return v

    @field_validator("xgb_max_depth")
    @classmethod
    def _v_depth(cls, v):
        if v is not None and not (2 <= v <= 12):
            raise ValueError("XGBoost max_depth 应在 2-12 之间")
        return v

    @field_validator("xgb_lr")
    @classmethod
    def _v_lr(cls, v):
        if v is not None and not (0.0001 <= v <= 1):
            raise ValueError("XGBoost 学习率应在 [0.0001, 1] 之间")
        return v

    @field_validator("xgb_n_est")
    @classmethod
    def _v_nest(cls, v):
        if v is not None and not (10 <= v <= 5000):
            raise ValueError("XGBoost 树数量应在 10-5000 之间")
        return v

    @model_validator(mode="after")
    def _v_soc_order(self):
        if self.soc_min is not None and self.soc_max is not None and self.soc_min >= self.soc_max:
            raise ValueError("最低SOC必须小于最高SOC")
        return self

    soc_min: Optional[int] = None
    soc_max: Optional[int] = None
    rated_power: Optional[int] = None
    price_peak: Optional[float] = None
    price_high: Optional[float] = None
    price_flat: Optional[float] = None
    price_valley: Optional[float] = None
    include_thermal: Optional[bool] = None
    include_degradation: Optional[bool] = None
    use_ml_forecast: Optional[bool] = None
    enable_dr: Optional[bool] = None
    orchestrator: Optional[str] = None
    xgb_max_depth: Optional[int] = None
    xgb_lr: Optional[float] = None
    xgb_n_est: Optional[int] = None
    xgb_temp_corr: Optional[bool] = None
    xgb_floor: Optional[bool] = None
    xgb_recursive: Optional[bool] = None
    clear_cache: bool = False


USER_CONFIG_KEYS = ["soc_min", "soc_max", "rated_power", "include_thermal", "include_degradation",
                    "use_ml_forecast", "enable_dr", "orchestrator", "price_peak", "price_high",
                    "price_flat", "price_valley", "xgb_max_depth", "xgb_lr", "xgb_n_est",
                    "xgb_temp_corr", "xgb_floor", "xgb_recursive"]


def _user_config_path():
    return os.path.join(ROOT, "config", "user_config.json")


@app.post("/api/params")
def set_params(req: ParamsReq):
    with _sess().lock:
        for k, v in req.model_dump(exclude_none=True).items():
            if k == "clear_cache":
                continue
            if k in _sess().params:
                _sess().params[k] = v
        if req.clear_cache:
            _sess().clear_solve_state()
    return {"ok": True, "params": _sess().params}


@app.post("/api/settings/save")
def settings_save():
    os.makedirs(os.path.dirname(_user_config_path()), exist_ok=True)
    cfg = {k: _sess().params.get(k) for k in USER_CONFIG_KEYS}
    with open(_user_config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return {"ok": True, "msg": f"配置已保存至 config{os.sep}user_config.json（{len(cfg)}项），重启后自动恢复"}


@app.post("/api/settings/reset")
def settings_reset():
    if os.path.exists(_user_config_path()):
        os.remove(_user_config_path())
    defaults = {"soc_min": 20, "soc_max": 90, "rated_power": 1000,
                "include_thermal": True, "include_degradation": True,
                "use_ml_forecast": True, "enable_dr": True, "orchestrator": "LangGraph",
                "price_peak": 1.35, "price_high": 1.05, "price_flat": 0.65, "price_valley": 0.32,
                "xgb_max_depth": 6, "xgb_lr": 0.1, "xgb_n_est": 200,
                "xgb_temp_corr": True, "xgb_floor": True, "xgb_recursive": True}
    with _sess().lock:
        _sess().params.update(defaults)
        _sess().clear_solve_state()
    return {"ok": True, "params": _sess().params}


# ---------------- DR 手动触发 ----------------
class DRReq(BaseModel):
    start: str
    end: str
    target: float
    subsidy: float
    dr_type: str  # 削峰 / 填谷 / 备用
    force: bool = False  # P1-07：与已有DR事件重叠时强制叠加


@app.post("/api/dr/trigger")
def dr_trigger(req: DRReq):
    import re as _re
    # P1-07：时间格式/顺序校验 + 与已有手动 DR 事件重叠阻断（force=true 强制叠加）
    for _t in (req.start, req.end):
        if not _re.match(r"^\d{2}:\d{2}$", _t):
            raise HTTPException(400, "时间格式应为 HH:MM（如 08:00）")
    _m = lambda t: int(t[:2]) * 60 + int(t[3:5])
    if _m(req.start) >= _m(req.end):
        raise HTTPException(400, "开始时间必须早于结束时间")
    # P1-B2 修复：补业务范围校验——此前仅 target>0，target=1e9 / subsidy=9999 /
    # 跨 8 小时的超长时段可被 curl 直调绕过前端 min/max，导致求解异常或经济损失。
    if req.target > 10000:
        raise HTTPException(400, "目标削减功率上限 10000 kW")
    if not (0 <= req.subsidy <= 20):
        raise HTTPException(400, "补贴单价应在 [0, 20] 元/kWh 之间")
    if _m(req.end) - _m(req.start) > 480:
        raise HTTPException(400, "DR 时段长度不能超过 8 小时")
    if req.target <= 0:
        raise HTTPException(400, "目标功率必须为正数")
    with _sess().lock:
        _overlaps = []
        for _sig in _sess().manual_dr_signals:
            _d, _s0 = str(_sig.start_time).split(" ", 1)
            _d2, _e0 = str(_sig.end_time).split(" ", 1)
            if _d != _sess().selected_date:
                continue
            if _m(_s0) < _m(req.end) and _m(req.start) < _m(_e0):
                _overlaps.append(f"{_s0}-{_e0}")
        if _overlaps and not req.force:
            return JSONResponse(
                {"ok": False, "overlap": True, "overlaps": _overlaps,
                 "msg": f"与已有DR事件时间重叠：{'、'.join(_overlaps)}；确需叠加请再次提交（force）"},
                status_code=409)
        sig = DRSignal(start_time=f"{_sess().selected_date} {req.start}", end_time=f"{_sess().selected_date} {req.end}",
                       target_reduction_kw=float(req.target), subsidy_per_kwh=float(req.subsidy),
                       dr_type={"削峰": "peak_shaving", "填谷": "valley_filling", "备用": "reserve"}.get(req.dr_type, "peak_shaving"))
        _sess().manual_dr_signals.append(sig)
        _sess().clear_solve_state()
    return {"ok": True, "msg": f"已触发DR事件：{req.start}-{req.end} · 目标{req.target:.0f}kW",
            "count": len(_sess().manual_dr_signals)}


# ---------------- 供应商管理 ----------------
@app.get("/api/providers")
def get_providers():
    # P0-02：API Key 不再回传明文——has_key + api_key_masked 脱敏
    safe = copy.deepcopy(_sess().mp_state)
    for _p in safe.get("providers", []):
        _k = _p.get("api_key", "")
        _p["has_key"] = bool(_k)
        _p["api_key_masked"] = auth_mod.mask_key(_k)
        _p.pop("api_key", None)
    return safe


class ProviderSaveReq(BaseModel):
    state: dict


@app.post("/api/providers/save")
def save_providers(req: ProviderSaveReq):
    # P0-02：前端脱敏后不回传明文 → api_key 为空的供应商保留原密钥（按 id 匹配）
    old_keys = {p.get("id"): p.get("api_key", "") for p in _sess().mp_state.get("providers", [])}
    for p in req.state.get("providers", []):
        p.pop("api_key_masked", None)
        p.pop("has_key", None)
        if not p.get("api_key") and p.get("id") in old_keys:
            p["api_key"] = old_keys[p["id"]]
    _sess().mp_state = req.state
    _sess().save_model_config()
    return {"ok": True}


class ProviderTestReq(BaseModel):
    provider: dict


@app.post("/api/providers/test")
def test_provider(req: ProviderTestReq):
    """= 原 _test_llm_connection：最小连通性测试。"""
    provider = req.provider
    stored = next((p.get("api_key", "") for p in _sess().mp_state.get("providers", [])
                   if p.get("id") == provider.get("id")), "")  # P0-02：脱敏回传时空 Key 回退存储值
    key = provider.get("api_key") or stored or os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        return {"ok": False, "msg": "未配置 API Key"}
    base = (provider.get("base_url") or "").rstrip("/")
    if not base:
        return {"ok": False, "msg": "Base URL 为空"}
    try:
        assert_safe_llm_url(base)  # P0-03 SSRF 防护（🟠#11：公共模块 src/utils/url_guard）
    except SSRFBlockedError as e:
        return {"ok": False, "msg": str(e)}
    model = provider["models"][0] if provider.get("models") else "deepseek-chat"
    t0 = time.time()
    try:
        body = json.dumps({"model": model, "max_tokens": 5,
                           "messages": [{"role": "user", "content": "ping"}]}).encode()
        if "Anthropic" in provider.get("api_format", ""):
            request = _ureq.Request(base + "/messages", data=body, method="POST",
                                    headers={"content-type": "application/json", "x-api-key": key,
                                             "anthropic-version": "2023-06-01"})
        else:
            request = _ureq.Request(base + "/chat/completions", data=body, method="POST",
                                    headers={"content-type": "application/json",
                                             "Authorization": f"Bearer {key}"})
        with _ureq.urlopen(request, timeout=15) as resp:
            resp.read()
        return {"ok": True, "msg": f"连接成功（{time.time()-t0:.1f}s · {model}）"}
    except Exception as e:
        detail = getattr(e, "read", lambda: b"")()
        try:
            detail = detail.decode("utf-8")[:160]
        except Exception:
            detail = ""
        return {"ok": False, "msg": f"失败：{e} {detail}".strip()}


# ---------------- AI 解释 ----------------
@app.post("/api/explain")
def explain():
    if not ensure_solved():
        raise HTTPException(409, "not solved")
    key, base, model, _ = _sess().active_llm()
    ctx = AgentContext(coordinator=_sess().coordinator, report=_sess().report, baseline=_sess().baseline,
                       viz_data=_sess().viz, dr_signals=_sess().dr_signals, historical_data=_sess().df,
                       selected_date=_sess().selected_date, engine=_sess().engine,
                       schedule=_sess().viz.get("final_schedule") if isinstance(_sess().viz, dict) else None,
                       llm_api_key=key, llm_base_url=base, llm_model=model)
    tools = SchedulingTools(ctx)
    out = tools.explain_day()
    # 透传来源（"llm" | "rule" | "none"）：调用方据此在界面上标注本次解释是
    # LLM 生成还是规则模板降级，无需解析正文前缀。
    return {"text": out, "source": tools.last_explain_source}


# ---------------- 对话 ----------------
class ChatReq(BaseModel):
    message: str


@app.post("/api/chat")
def chat(req: ChatReq):
    if not ensure_solved():
        raise HTTPException(409, "not solved")
    _sess().chat_history.append({"role": "user", "content": req.message})
    key, base, model, _ = _sess().active_llm()
    ctx = AgentContext(coordinator=_sess().coordinator, report=_sess().report, baseline=_sess().baseline,
                       viz_data=_sess().viz, dr_signals=_sess().dr_signals, historical_data=_sess().df,
                       selected_date=_sess().selected_date, engine=_sess().engine,
                       schedule=_sess().viz.get("final_schedule") if isinstance(_sess().viz, dict) else None,
                       llm_api_key=key, llm_base_url=base, llm_model=model)
    agent = create_agent(ctx, api_key=key, base_url=base, model=model)
    try:
        response = agent.respond(req.message)
    except Exception as e:
        response = f"⚠️ 对话Agent异常：{type(e).__name__}: {e}"
    # 🟠#19：chat_history 只 append 不裁剪 → 环形上限 200 条（返回窗口仍是最近 16 条）
    s = _sess()
    s.chat_history.append({"role": "assistant", "content": response})
    if len(s.chat_history) > 200:
        del s.chat_history[:-200]
    # 与原版一致：工具可能重跑调度，同步最新结果（🔴#4：回写纳入会话锁，消除写竞争）
    if ctx.report is not None and ctx.report is not s.report:
        with s.lock:
            s.coordinator, s.report, s.baseline, s.viz = ctx.coordinator, ctx.report, ctx.baseline, ctx.viz_data
    return {"reply": response, "history": s.chat_history[-16:],
            "mode": getattr(agent, "mode", "unknown")}


@app.post("/api/chat/stream")
async def chat_stream(req: ChatReq):
    """流式对话（SSE）。

    协议：逐条 `data: {"delta": "..."}`，结束发 `data: {"done": true, "history": [...]}`。
    是否真正逐字由 llm_params.stream 决定——前端只实现一套解析逻辑：
    开关关闭时服务端一次性发出整段 delta，视觉上等同「整段返回」。
    """
    if not ensure_solved():
        raise HTTPException(409, "not solved")

    # ⚠️ contextvar（_CURRENT_SESSION）在流式生成器的执行上下文中不保证可见，
    # 因此在进入生成器之前把会话对象捕获进闭包，生成器内一律用 s。
    s = _sess()
    s.chat_history.append({"role": "user", "content": req.message})
    key, base, model, params = s.active_llm()
    ctx = AgentContext(coordinator=s.coordinator, report=s.report, baseline=s.baseline,
                       viz_data=s.viz, dr_signals=s.dr_signals, historical_data=s.df,
                       selected_date=s.selected_date, engine=s.engine,
                       schedule=s.viz.get("final_schedule") if isinstance(s.viz, dict) else None,
                       llm_api_key=key, llm_base_url=base, llm_model=model)
    agent = create_agent(ctx, api_key=key, base_url=base, model=model)
    use_stream = bool(params.get("stream")) and callable(getattr(agent, "astream", None))

    def _sse(obj) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    async def gen():
        acc = []
        try:
            if use_stream:
                async for delta in agent.astream(req.message):
                    acc.append(delta)
                    yield _sse({"delta": delta})
            else:
                text = agent.respond(req.message)
                acc.append(text)
                yield _sse({"delta": text})
        except Exception as e:
            msg = f"⚠️ 对话Agent异常：{type(e).__name__}: {e}"
            acc.append(msg)
            yield _sse({"delta": msg})

        response = "".join(acc) or "（空回复）"
        s.chat_history.append({"role": "assistant", "content": response})
        if len(s.chat_history) > 200:
            del s.chat_history[:-200]
        # 与 /api/chat 一致：工具可能重跑调度 → 同步最新结果
        if ctx.report is not None and ctx.report is not s.report:
            with s.lock:
                s.coordinator, s.report, s.baseline, s.viz = (
                    ctx.coordinator, ctx.report, ctx.baseline, ctx.viz_data)
        yield _sse({"done": True, "history": s.chat_history[-16:],
                    "mode": getattr(agent, "mode", "unknown")})

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chat/clear")
def chat_clear():
    _sess().chat_history = [{"role": "assistant",
                       "content": "对话已清空。可以问我收益、温度、调度原因，或让我调参重跑。"}]
    return {"ok": True, "history": _sess().chat_history}


@app.get("/api/chat/history")
def chat_history():
    return {"history": _sess().chat_history[-16:]}


# ---------------- 认证（P0-02） ----------------
class LoginReq(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def auth_login(req: LoginReq, request: Request):
    # 🟠#21：IP+账号维度限流与指数退避锁定
    ip = request.client.host if request.client else "unknown"
    gate_key = _login_gate_key(ip, req.username[:64])
    remaining = _login_rate_limited(gate_key)
    if remaining is not None:
        _log.warning("登录被限流：ip=%s 账号=%s（剩余 %ss）", ip, req.username[:32], remaining)
        return JSONResponse(
            {"ok": False, "detail": f"尝试过于频繁，请 {remaining} 秒后重试"},
            status_code=429, headers={"Retry-After": str(remaining)})
    if not AUTH.verify(req.username, req.password):
        _login_record_fail(gate_key)
        return JSONResponse({"ok": False, "detail": "用户名或密码错误"}, status_code=401)
    _login_record_success(gate_key)
    # P0-F2：token 绑定 User-Agent 指纹（sha256 前 16 hex），降低 XSS 窃取后的可用性
    token = auth_mod.jwt_encode({
        "sub": req.username,
        "ua_fp": auth_mod.ua_fingerprint(request.headers.get("user-agent", "")),
    })
    return {"ok": True, "token": token, "expires_in": 12 * 3600,
            "must_change": AUTH.must_change(), "username": req.username}


class ChangePwdReq(BaseModel):
    old_password: str
    new_password: str


@app.post("/api/auth/change-password")
def auth_change_password(req: ChangePwdReq):
    if not AUTH.verify(AUTH.username, req.old_password):
        raise HTTPException(400, "原口令错误")
    if len(req.new_password) < 8:
        raise HTTPException(400, "新口令至少 8 位")
    try:
        AUTH.set_password(req.new_password)
    except auth_mod.AuthModeError as e:
        # env 模式下口令由环境变量托管，应用内改密无意义——给出可执行的改法而不是 500
        raise HTTPException(400, str(e))
    return {"ok": True, "msg": "口令已更新，请使用新口令重新登录"}


@app.get("/api/system-info")
def system_info():
    import platform
    try:
        from importlib.metadata import version as pv
        lg_ver = pv("langgraph")
    except Exception:
        lg_ver = "N/A"
    return {"version": __version__, "py": f"{sys.version_info.major}.{sys.version_info.minor}", "langgraph": lg_ver}


# 静态前端
# P0-B2 修复：显式白名单静态文件扩展名——此前整个 web 目录无条件挂载，一旦部署时
# 误将 config/、.solve_cache/ 等敏感目录放入 web/，任意文件可被 GET 拉取。
# 现在非白名单扩展名一律 403（配合 lifespan 启动扫描告警，默认安全）。
_STATIC_ALLOWED_EXT = {".html", ".css", ".js", ".map", ".svg", ".png", ".jpg", ".ico",
                       ".woff", ".woff2", ".ttf", ".txt", ".md"}


class _GuardedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        ext = os.path.splitext(path)[1].lower()
        if ext and ext not in _STATIC_ALLOWED_EXT:
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
        return await super().get_response(path, scope)


app.mount("/", _GuardedStaticFiles(directory=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"), html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    # 🟡#33：host/port/日志级别改环境变量覆盖，公网部署不再必须改代码
    _host = os.getenv("ENERGY_HOST", "127.0.0.1")
    _port = int(os.getenv("ENERGY_PORT", "8800"))
    _log_level = os.getenv("ENERGY_LOG_LEVEL", "info")
    log.info(f"储能调度系统 Web · http://{_host}:{_port}")
    uvicorn.run(app, host=_host, port=_port, log_level=_log_level)
