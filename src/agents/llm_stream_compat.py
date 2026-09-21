# -*- coding: utf-8 -*-
"""OpenAI 兼容网关（DeepSeek 等）与 `ChatOpenAI` 之间的两处适配。

`ChatOpenAI` 只按官方 OpenAI 规范解析响应，DeepSeek 的两处差异因此都落在调用方：

**1. `reasoning_content` 双向丢失**
模块级警告就写着非标准字段（`reasoning_content` / `reasoning_details`）不会被提取；
流式路径上 `_convert_delta_to_message_chunk` 的 `additional_kwargs` 是个只装
`function_call` 的空 dict。后果：思考阶段前端只能干等（正文一个字没有），
而思考模型还要求多轮/工具调用时把上一轮 assistant 的 `reasoning_content` 原样带回，
否则服务端直接报错。官方生态的 `langchain-deepseek` 只补入向、不补回传，
且不在本项目依赖里——两头都在这儿自己接。

**2. 流式 `tool_calls` 缺 `index` → 空名工具调用**
OpenAI 规范里 tool_calls 的增量块带 `index` 用于分组，DeepSeek 只给第一个分片带。
`langchain_core` 按下标合并，缺 index 的分片被当成**另一条**调用，实测产出
`{'name': '', 'args': {}}`，LangGraph 拿着空名去查工具表就报 unknown tool。
按规范"不带 index 即第一条"补 0 即可让分片累加回同一条调用。

字段/键不存在时全部原样返回，所以对非思考模型（`deepseek-chat` 这类）完全无感，
不会给请求多出任何键。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

__all__ = ["chat_model_class", "reasoning_from_chunk_dict",
           "normalize_tool_call_indexes", "attach_reasoning_to_payload"]


def _pick_reasoning(d: Optional[dict]) -> str:
    """从一个 delta/message 字典里取思维链片段。

    `reasoning_content` 是 DeepSeek 的字段名，`reasoning` 是 OpenRouter 一类网关的
    别名；只认非空字符串，别的类型当没有——宁可少显示，不可拼出乱码。
    """
    if not isinstance(d, dict):
        return ""
    for key in ("reasoning_content", "reasoning"):
        v = d.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _first_choice(chunk: Any) -> Optional[dict]:
    if not isinstance(chunk, dict):
        return None
    choices = chunk.get("choices") or (chunk.get("chunk") or {}).get("choices") or []
    return choices[0] if choices and isinstance(choices[0], dict) else None


def reasoning_from_chunk_dict(chunk: Any) -> str:
    """从流式原始 chunk（`model_dump()` 之后的 dict）取思维链增量。

    openai SDK 的响应模型是 `extra="allow"` 的 pydantic，`model_dump()` 会把
    `reasoning_content` 留在 dict 里——langchain 不读它，我们自己读。
    """
    choice = _first_choice(chunk)
    return _pick_reasoning((choice or {}).get("delta"))


def normalize_tool_call_indexes(chunk: Any) -> bool:
    """就地把缺失的 `tool_calls[*].index` 补成 0，返回是否有改动。

    只补"键不存在"的情况；显式给了 index（哪怕是 0 以外的值）一律不动，
    多路并行调用的分组信息是提供方给的，我们不猜。
    """
    choice = _first_choice(chunk)
    calls = (choice or {}).get("delta", {})
    calls = calls.get("tool_calls") if isinstance(calls, dict) else None
    if not isinstance(calls, list):
        return False
    changed = False
    for call in calls:
        if isinstance(call, dict) and "index" not in call:
            call["index"] = 0
            changed = True
    return changed


def attach_reasoning_to_payload(messages: list, payload: dict) -> dict:
    """把 `additional_kwargs["reasoning_content"]` 回写到请求体的 assistant 消息上。

    chat/completions 分支下 `payload["messages"]` 与入参 `messages` 严格 1:1
    （`_get_request_payload` 逐条转换）。按下标配对后再校验 role 一致，
    对不上就整体放弃——宁可不回传，也不能把思维链安到别人的消息上。
    """
    out = payload.get("messages")
    if not isinstance(out, list) or len(out) != len(messages):
        return payload
    for src, dst in zip(messages, out):
        if not isinstance(dst, dict) or dst.get("role") != "assistant":
            continue
        text = _pick_reasoning(getattr(src, "additional_kwargs", None))
        if text and not dst.get("reasoning_content"):
            dst["reasoning_content"] = text
    return payload


@lru_cache(maxsize=1)
def chat_model_class():
    """返回（并缓存）适配子类。延迟导入：langchain 缺失时 `chat_agent` 要能降级。"""
    from langchain_openai import ChatOpenAI

    class StreamCompatChatOpenAI(ChatOpenAI):
        """reasoning_content 入向提取 + 出向回传 + tool_calls index 兜底。"""

        def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class,
                                               base_generation_info):
            # 先补 index 再交给父类：父类是从原始 dict 造 tool_call_chunks 的，
            # 生成完再改就来不及了。
            normalize_tool_call_indexes(chunk)
            gen = super()._convert_chunk_to_generation_chunk(
                chunk, default_chunk_class, base_generation_info)
            if gen is not None:
                text = reasoning_from_chunk_dict(chunk)
                if text:
                    gen.message.additional_kwargs["reasoning_content"] = text
            return gen

        def _create_chat_result(self, response, generation_info=None):
            result = super()._create_chat_result(response, generation_info)
            try:
                choices = getattr(response, "choices", None) or []
                msg = getattr(choices[0], "message", None) if choices else None
                extra = getattr(msg, "model_extra", None)
                if not extra and msg is not None and hasattr(msg, "model_dump"):
                    extra = msg.model_dump()
                text = _pick_reasoning(extra)
                if text and result.generations:
                    result.generations[0].message.additional_kwargs[
                        "reasoning_content"] = text
            except Exception:  # 非流式路径取不到就放弃，不能因此让整个调用失败
                pass
            return result

        def _get_request_payload(self, input_, *, stop=None, **kwargs):
            payload = super()._get_request_payload(input_, stop=stop, **kwargs)
            try:
                attach_reasoning_to_payload(
                    self._convert_input(input_).to_messages(), payload)
            except Exception:
                pass
            return payload

    return StreamCompatChatOpenAI
