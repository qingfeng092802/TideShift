"""工具调用记录器 —— 评测的"眼见为实"层。

为什么不让评测去解析模型返回的 tool_calls：那测到的是"模型说了什么"，不是
"系统实际执行了什么"。中间还隔着参数校验、异常降级、规则路由。本模块直接包在
SchedulingTools 的方法上，记录**真实发生过**的调用，规则模式与 LLM 模式共用一套。
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from evals.metrics import TOOL_NAMES, ToolCall

# 被替换掉的重活工具：跑一次 MILP 要 38~43 秒，而评测只关心"参数解析对不对"，
# 不关心重跑结果。默认把 run_with_params 打桩，一次 eval 才可能压进 CI。
DEFAULT_STUB_TOOLS: Sequence[str] = ("run_with_params",)

_STUB_PREFIX = "【评测桩】未调用求解器，仅确认参数已解析为："


@dataclass
class Recorder:
    calls: List[ToolCall] = field(default_factory=list)
    _restore: List[Any] = field(default_factory=list)

    def executed_tools(self) -> List[str]:
        return [c.tool for c in self.calls]

    def reset(self) -> None:
        self.calls.clear()

    def uninstall(self) -> None:
        for obj, name, original in self._restore:
            setattr(obj, name, original)
        self._restore.clear()


def _normalize_args(func: Callable[..., Any], args: tuple,
                    kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """把位置参数与关键字参数统一成关键字，便于按 expect_args 断言。

    取值为 None 的参数被剔除：在本项目里 None 一律表示"这次没传"，
    留在记录里会让 {"soc_max": 80} 这种断言写成八项比对，读不出实际解析结果。
    """
    try:
        bound = inspect.signature(func).bind(*args, **kwargs)
        bound.apply_defaults()
    except (TypeError, ValueError):
        return {k: v for k, v in kwargs.items() if v is not None}
    return {k: v for k, v in bound.arguments.items()
            if k != "self" and v is not None}


def _stub_run_with_params(**kwargs: Any) -> str:
    shown = ", ".join(f"{k}={v}" for k, v in sorted(kwargs.items()) if v is not None)
    return f"{_STUB_PREFIX}{shown}" if shown else f"{_STUB_PREFIX}（未提取到任何参数）"


def install(tools: Any, stub_tools: Optional[Iterable[str]] = None) -> Recorder:
    """就地包装 tools 上的全部已知工具，返回可 reset/uninstall 的 Recorder。"""
    rec = Recorder()
    stub = set(DEFAULT_STUB_TOOLS if stub_tools is None else stub_tools)

    for name in TOOL_NAMES:
        original = getattr(tools, name, None)
        if original is None:
            continue
        if name in stub:
            wrapped: Callable[..., Any] = (
                _stub_run_with_params if name == "run_with_params" else (lambda **kw: "【评测桩】")
            )
        else:
            wrapped = original

        def make(tool_name: str, func: Callable[..., Any],
                 signature_from: Callable[..., Any]) -> Callable[..., Any]:
            def wrapper(*args: Any, **kwargs: Any) -> str:
                call = ToolCall(
                    tool=tool_name,
                    # 参数一律按**真实方法**的签名归一化：打桩函数的 **kwargs 形态
                    # 会把 soc_max=80 记成 kwargs 字典，评测断言的就不是同一个口径了。
                    args=_normalize_args(signature_from, args, kwargs),
                    seq=len(rec.calls) + 1,
                )
                try:
                    call.returned = func(*args, **kwargs)
                except Exception as exc:  # 工具自己炸了也要留痕，不能把异常吞进指标里
                    call.returned = f"【工具异常】{type(exc).__name__}: {exc}"
                    call.args["_raised"] = type(exc).__name__
                rec.calls.append(call)
                return call.returned

            return wrapper

        setattr(tools, name, make(name, wrapped, original))
        rec._restore.append((tools, name, original))

    return rec
