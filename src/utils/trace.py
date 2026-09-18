"""运行追溯（trace）—— 回答"这一次到底发生了什么"。

为什么需要它：agent 系统跑坏时，日志只能告诉你"哪里报了错",看不出**一次运行**里
每个环节的耗时分布、LLM 走了哪条降级路径、哪一步把收益算变了。没有 trace，
调试方式只剩重跑一遍再看。

设计取舍：
- **JSONL 落盘 + 进程内有界环形**：文件给"事后查历史"，环形缓冲给看板实时读，
  两者都不需要额外服务（不引 Langfuse/OTel，与项目"完全离线可用"的口径一致）。
- **默认不记录业务内容**。`ENERGY_TRACE_LEVEL` 三档：
  `off` 只留开关、`basic`（默认）记事件名/耗时/状态/计数、`full` 才记入参与回答文本。
  对话内容可能含用户自己的负荷数据，默认不外泄到磁盘。
- **无 run 时零开销**。所有 `span()` 在没有活动 run 时直接空转，
  所以 import 本模块不会改变任何既有行为，测试也不需要先建 run。

环境变量：
    ENERGY_TRACE_DIR     trace 文件目录，默认 <项目根>/logs/traces
    ENERGY_TRACE_LEVEL   off | basic | full，默认 basic
    ENERGY_TRACE_MAX     进程内保留的 run 数，默认 64
"""
from __future__ import annotations

import functools
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEVELS = ("off", "basic", "full")

# 活动 run 必须是**线程局部**：求解跑在后台线程（start_solve._worker），
# 用模块级全局会把并发两个请求的事件混进同一个 run，trace 就成了假证据。
_state = threading.local()
_RECENT: "List[RunTrace]" = []
_RECENT_LOCK = threading.Lock()


def _get_current() -> Optional["RunTrace"]:
    return getattr(_state, "current", None)


def _set_current(rt: Optional["RunTrace"]) -> None:
    _state.current = rt


def _level() -> str:
    raw = (os.getenv("ENERGY_TRACE_LEVEL") or "basic").strip().lower()
    return raw if raw in LEVELS else "basic"


def _max_kept() -> int:
    try:
        return max(1, int(os.getenv("ENERGY_TRACE_MAX") or "64"))
    except ValueError:
        return 64


def trace_dir() -> Path:
    return Path(os.getenv("ENERGY_TRACE_DIR") or (PROJECT_ROOT / "logs" / "traces"))


def traced(kind: str, **attrs: Any):
    """把一个函数整体包成一次可追溯运行。

    为什么用装饰器而不是在端点里写 `with trace.run(...)`：FastAPI 的同步端点跑在
    threadpool 里，中间件线程开的 run 对它不可见；而逐个端点缩进包一层会造出
    大片无意义 diff。装饰器保住函数体不动，functools.wraps 保住签名，
    FastAPI 仍能按原签名注入请求模型。
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with run(kind, **attrs) as rt:
                out = fn(*args, **kwargs)
                if rt is not None and isinstance(out, dict):
                    rt.attrs["reply_keys"] = sorted(out)[:12]
                return out
        return wrapper
    return deco


def current_run() -> Optional["RunTrace"]:
    return _get_current()


@dataclass
class Span:
    name: str
    start_mono: float
    dur_ms: float = 0.0
    status: str = "ok"
    attrs: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out = {"name": self.name, "dur_ms": round(self.dur_ms, 1), "status": self.status}
        if self.attrs:
            out["attrs"] = self.attrs
        return out


@dataclass
class RunTrace:
    """一次可追溯的运行：一次求解、一轮对话、一次寻优循环。"""
    kind: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: str = field(default_factory=lambda: datetime.now(
        timezone.utc).astimezone().isoformat(timespec="seconds"))
    started_mono: float = field(default_factory=time.perf_counter)
    spans: List[Span] = field(default_factory=list)
    attrs: Dict[str, Any] = field(default_factory=dict)
    finished_s: float = 0.0
    status: str = "running"

    def add(self, span: Span) -> None:
        self.spans.append(span)

    def total_ms(self) -> float:
        return (time.perf_counter() - self.started_mono) * 1000.0

    def summary(self) -> Dict[str, Any]:
        slowest = max(self.spans, key=lambda s: s.dur_ms, default=None)
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "started_at": self.started_at,
            "status": self.status,
            "duration_ms": round(self.finished_s or self.total_ms(), 1),
            "n_spans": len(self.spans),
            "errors": sum(1 for s in self.spans if s.status != "ok"),
            "slowest_span": (slowest.name if slowest else None),
            "slowest_ms": (round(slowest.dur_ms, 1) if slowest else 0.0),
            "attrs": self.attrs,
        }

    def detail(self) -> Dict[str, Any]:
        out = self.summary()
        out["spans"] = [s.to_dict() for s in self.spans]
        return out


@contextmanager
def run(kind: str, **attrs: Any) -> Iterator[Optional[RunTrace]]:
    """开一段 run。嵌套调用时以最外层为准（内层复用当前 run，不另开）。"""
    if _level() == "off":
        yield None
        return
    if current_run() is not None:          # 已在一轮运行里：不重复开
        yield current_run()
        return
    rt = RunTrace(kind=kind, attrs=dict(attrs))
    _set_current(rt)
    try:
        yield rt
    except Exception as exc:
        rt.status = f"error:{type(exc).__name__}"
        raise
    else:
        if rt.status == "running":
            rt.status = "ok"
    finally:
        rt.finished_s = rt.total_ms()
        _set_current(None)
        _keep(rt)


def _keep(rt: RunTrace) -> None:
    with _RECENT_LOCK:
        _RECENT.append(rt)
        while len(_RECENT) > _max_kept():
            _RECENT.pop(0)
    try:
        d = trace_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"trace-{datetime.now().strftime('%Y%m%d')}.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rt.detail(), ensure_ascii=False) + "\n")
    except OSError:
        pass    # 盘不可用不能让追溯把主流程拖挂


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Optional[Span]]:
    """记录一段耗时。没有活动 run 时完全空转（零开销、不抛错）。"""
    rt = current_run()
    if rt is None or _level() == "off":
        yield None
        return
    sp = Span(name=name, start_mono=time.perf_counter())
    payload = redact(attrs)
    if payload:
        sp.attrs = payload
    try:
        yield sp
    except Exception as exc:
        sp.status = f"error:{type(exc).__name__}"
        raise
    finally:
        sp.dur_ms = (time.perf_counter() - sp.start_mono) * 1000.0
        rt.add(sp)


def redact(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """按级别裁剪：basic 只留标量计数与短标识，full 才留文本内容。"""
    level = _level()
    out: Dict[str, Any] = {}
    for key, value in attrs.items():
        if value is None or isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str):
            if level == "full":
                out[key] = value[:2000]
            else:
                out[f"{key}_len"] = len(value)
        elif isinstance(value, (list, tuple)):
            out[f"{key}_n"] = len(value)
        elif isinstance(value, dict):
            out[f"{key}_keys"] = sorted(str(k) for k in value)[:12]
    return out


def recent(limit: int = 20) -> List[Dict[str, Any]]:
    with _RECENT_LOCK:
        window = list(_RECENT)[-limit:]
    return [rt.summary() for rt in reversed(window)]


def find(run_id: str) -> Optional[RunTrace]:
    with _RECENT_LOCK:
        snapshot = list(_RECENT)
    for rt in reversed(snapshot):
        if rt.run_id == run_id:
            return rt
    return None


def reset() -> None:
    """测试用：清空进程内环形与当前线程的 run。文件里的历史记录不动。"""
    with _RECENT_LOCK:
        _RECENT.clear()
    _set_current(None)
