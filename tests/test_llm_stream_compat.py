# -*- coding: utf-8 -*-
"""OpenAI 兼容网关适配层 + Base URL 归一化的回归测试。

守的四件事（都对应实测过的症状，不是假想）：
1. `normalize_llm_base_url`：只补"路径为空"这一种，别猜自定义网关路径。
2. 思维链入向：`ChatOpenAI` 会丢掉 `reasoning_content`，必须我们自己抬进
   `additional_kwargs`，否则前端在思考阶段只能看空气泡。
3. 思维链出向：思考模型要求把上一轮 `reasoning_content` 带回，少了就报错。
4. 流式 `tool_calls` 缺 `index`：langchain 按下标合并，缺下标的分片会被当成
   另一条调用，实测产出 `name=""`，LangGraph 拿空名查工具表就报 unknown tool。
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.url_guard import normalize_llm_base_url
from src.agents.llm_stream_compat import (attach_reasoning_to_payload,
                                          chat_model_class,
                                          normalize_tool_call_indexes,
                                          reasoning_from_chunk_dict)


@pytest.mark.parametrize("raw,want", [
    ("https://api.deepseek.com", "https://api.deepseek.com/v1"),      # 文档写法 A
    ("https://api.deepseek.com/", "https://api.deepseek.com/v1"),
    ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1"),   # 文档写法 B
    ("https://api.deepseek.com/v1/", "https://api.deepseek.com/v1"),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1",
     "https://dashscope.aliyuncs.com/compatible-mode/v1"),            # 已有路径不猜
    ("https://gw.corp.example/api/llm/v3", "https://gw.corp.example/api/llm/v3"),
    ("", ""),
    ("not a url", "not a url"),          # 解析不出主机名就原样交回，让 SSRF 校验去报错
])
def test_base_url_normalization(raw, want):
    assert normalize_llm_base_url(raw) == want


def test_reasoning_extracted_from_stream_delta():
    chunk = {"choices": [{"delta": {"reasoning_content": "先看价格档"}}]}
    assert reasoning_from_chunk_dict(chunk) == "先看价格档"
    # 正文增量里没有思维链字段时返回空串，不能凭空造事件
    assert reasoning_from_chunk_dict({"choices": [{"delta": {"content": "今日"}}]}) == ""
    assert reasoning_from_chunk_dict(None) == ""
    assert reasoning_from_chunk_dict({"choices": []}) == ""
    # 非字符串（个别网关给 dict/None）一律当没有，宁可不显示也不拼出乱码
    assert reasoning_from_chunk_dict({"choices": [{"delta": {"reasoning_content": {"a": 1}}}]}) == ""


def test_missing_tool_call_index_is_backfilled():
    """不带 index 的分片补 0；显式给了 index 的（多路并行）一个都不许动。

    ⚠️ 这里必须用 OpenAI 的**线上格式**（name/arguments 嵌在 `function` 下）。
    langchain 的 `_convert_delta_to_message_chunk` 取 `rtc["index"]` 用的是下标访问，
    整段推导包在 `except KeyError: pass` 里——缺 index 时不是"合并错组"，
    而是**整条 tool_call 被静默丢掉**，比空名字更难查。
    """
    chunk = {"choices": [{"delta": {"tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "get_report", "arguments": ""}}]}}]}
    assert normalize_tool_call_indexes(chunk) is True
    assert chunk["choices"][0]["delta"]["tool_calls"][0]["index"] == 0

    parallel = {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"name": "a", "arguments": ""}},
        {"index": 1, "function": {"name": "b", "arguments": ""}}]}}]}
    normalize_tool_call_indexes(parallel)
    assert [c["index"] for c in parallel["choices"][0]["delta"]["tool_calls"]] == [0, 1]
    assert normalize_tool_call_indexes({"choices": [{"delta": {}}]}) is False


def test_reasoning_round_trip_through_payload():
    msgs = [SimpleNamespace(additional_kwargs={}),
            SimpleNamespace(additional_kwargs={"reasoning_content": "第一段想法"})]
    payload = {"messages": [{"role": "user", "content": "q"},
                            {"role": "assistant", "content": "", "tool_calls": []}]}
    attach_reasoning_to_payload(msgs, payload)
    assert payload["messages"][0].get("reasoning_content") is None
    assert payload["messages"][1]["reasoning_content"] == "第一段想法"

    # 长度对不齐就整体放弃：宁可不回传，也不能把思维链安到别人的消息上
    other = {"messages": [{"role": "assistant", "content": "x"}]}
    attach_reasoning_to_payload(msgs, other)
    assert "reasoning_content" not in other["messages"][0]
    # 非 assistant 消息即使位置对上也不写
    sysish = {"messages": [{"role": "user", "content": "q"},
                           {"role": "tool", "content": "r"}]}
    attach_reasoning_to_payload(msgs, sysish)
    assert all("reasoning_content" not in m for m in sysish["messages"])


# ========== 真子类：证明三个 hook 名在当前 langchain-openai 下确实被调到 ==========

@pytest.fixture(scope="module")
def model():
    ChatOpenAI = pytest.importorskip("langchain_openai").ChatOpenAI
    cls = chat_model_class()
    assert issubclass(cls, ChatOpenAI)
    return cls(api_key="sk-not-a-real-key", model="deepseek-chat",
               base_url="https://api.deepseek.com/v1")


def test_subclass_lifts_reasoning_and_index(model):
    from langchain_core.messages import AIMessageChunk

    raw = {"choices": [{"delta": {
        "reasoning_content": "想一下", "content": "今日",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "get_report", "arguments": "{}"}}]}}]}
    gen = model._convert_chunk_to_generation_chunk(raw, AIMessageChunk, {})
    assert gen.message.additional_kwargs["reasoning_content"] == "想一下"
    assert gen.message.content == "今日"
    assert gen.message.tool_call_chunks[0]["index"] == 0
    # 补上 index 后 langchain 才真的产出这条调用（缺 index 时整条被丢）
    assert gen.message.tool_calls[0]["name"] == "get_report"


def test_subclass_injects_reasoning_into_request(model):
    from langchain_core.messages import AIMessage, HumanMessage

    ai = AIMessage(content="", additional_kwargs={"reasoning_content": "上一轮的想法"},
                   tool_calls=[{"name": "get_report", "args": {}, "id": "c1", "type": "tool_call"}])
    payload = model._get_request_payload([HumanMessage(content="今天收益多少"), ai])
    assistant = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert assistant and assistant[0]["reasoning_content"] == "上一轮的想法"


def test_merged_tool_calls_keep_one_name(model):
    """补 index 之后，DeepSeek 式分片流合并出的是**一次**完整调用。

    不补 index 的两种实测后果：`rtc["index"]` 直接 KeyError → 整条调用被丢；
    或分片各自成组 → 合并出 `{'name': '', 'args': {}}` 的第二条，
    LangGraph 拿空名去查工具表就是 "unknown tool"。
    """
    from langchain_core.messages import AIMessageChunk

    def to_chunk(delta):
        raw = {"choices": [{"delta": delta}]}
        return model._convert_chunk_to_generation_chunk(
            raw, AIMessageChunk, {}).message

    def frag(arguments, **extra):
        return {"tool_calls": [dict({"type": "function",
                                     "function": {"arguments": arguments}}, **extra)]}

    m = to_chunk(frag("", id="c1", function={"name": "explain_schedule", "arguments": ""}))
    for piece in ('{"hour"', ": 15}"):
        m = m + to_chunk(frag(piece))
    assert len(m.tool_calls) == 1, m.tool_call_chunks
    assert m.tool_calls[0]["name"] == "explain_schedule"
    assert m.tool_calls[0]["args"] == {"hour": 15}
    assert not m.invalid_tool_calls


def test_langchain_core_accumulates_tool_call_args_by_index():
    """库层事实：tool_call 分片是按 index **累加**的，不是覆盖。

    这是"我们到底该不该自己拼 SSE"的判断依据——langchain-core 已经做对了分组，
    自己再写一份合并逻辑只会多一处会错的代码。所以本仓库不解析 OpenAI 的 SSE，
    只补它缺的那个 index（见下一条）。
    """
    from langchain_core.messages import AIMessageChunk

    def chunk(**kw):
        return AIMessageChunk(content="", tool_call_chunks=[
            dict({"type": "tool_call"}, **kw)])

    m = (chunk(name="explain_schedule", args="", id="c1", index=0)
         + chunk(name=None, args='{"hour"', id=None, index=0)
         + chunk(name=None, args=": 15}", id=None, index=0))
    assert len(m.tool_calls) == 1, m.tool_call_chunks
    assert m.tool_calls[0]["name"] == "explain_schedule"
    assert m.tool_calls[0]["args"] == {"hour": 15}

    # 并行两路调用靠 index 分组，互不污染
    two = (chunk(name="a", args="{}", id="1", index=0)
           + chunk(name="b", args="{}", id="2", index=1))
    assert [t["name"] for t in two.tool_calls] == ["a", "b"]


def test_langchain_core_needs_index_or_the_call_is_lost():
    """缺 index 时库层的两种失败：整条调用被丢，或分裂出一条空名调用。

    留这条是为了让 `normalize_tool_call_indexes` 有存在理由（不是"顺手加的兼容"）。
    ⚠️ 如果哪天它失败、且原因是上游不再 KeyError／不再分裂，那就是**可以删掉
    我们的补 0 逻辑**的信号，而不是要改掉这个断言——所以断言写的是"必须靠补 0
    才成立"的对照，而不是硬编码上游的错误输出长什么样。
    """
    from langchain_core.messages import AIMessageChunk

    raw = {"choices": [{"delta": {"tool_calls": [
        {"id": "c1", "type": "function", "index": 0,
         "function": {"name": "get_dr_info", "arguments": "{}"}}]}}]}
    no_index = [{k: v for k, v in c.items() if k != "index"}
                for c in raw["choices"][0]["delta"]["tool_calls"]]
    assert all("index" not in c for c in no_index)

    # 走 langchain 的 delta 转换：缺 index 的 tool_calls 不会产出任何调用
    from langchain_openai.chat_models.base import _convert_delta_to_message_chunk

    lost = _convert_delta_to_message_chunk({"tool_calls": no_index}, AIMessageChunk)
    assert lost.tool_call_chunks == [], "上游行为变了：该重新评估补 0 逻辑是否还需要"
    # 对照：同一个分片补上 index 就能正常产出
    got = _convert_delta_to_message_chunk(raw["choices"][0]["delta"], AIMessageChunk)
    assert got.tool_call_chunks[0]["name"] == "get_dr_info"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
