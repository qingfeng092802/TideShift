"""
LLM 决策解释层测试

测试策略：
- 全部用 mock 客户端，不联网、不需要 API Key
- 重点覆盖三件事：事实摘要正确、防幻觉校验能抓到编造数字、任何失败都能降级
"""
import io
import json
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents.llm_explainer import (
    DEFAULT_MODEL,
    DecisionDigest,
    ExplanationResult,
    LLMError,
    LLMExplainer,
    build_digest,
    call_chat_completions,
    check_grounding,
    resolve_llm_credentials,
    summarize_windows,
    template_explain,
)


# ========== 造数据 ==========
def _make_power():
    """谷段(0-6h)充电，晚高峰(18-22h)放电，其余待机"""
    p = np.zeros(96)
    p[0:24] = -1000.0      # 00:00-06:00 充电
    p[72:88] = 800.0       # 18:00-22:00 放电
    return p


def _make_price():
    pr = np.ones(96) * 0.65
    pr[0:24] = 0.32        # 谷
    pr[72:88] = 1.05       # 峰
    return pr


def _make_schedule(net_power=None):
    net = _make_power() if net_power is None else net_power
    return SimpleNamespace(
        net_power_kw=net,
        battery_temp_c=np.linspace(30, 44, 96),
        soc=np.linspace(0.5, 0.5, 97),
        charge_energy_kwh=2400.0,
        discharge_energy_kwh=1600.0,
        equivalent_cycles=1.2345,
        terminal_soc=0.5,
        energy_balance_error_kwh=0.0,
        soc_violation_steps=0,
        solver_status="Optimal",
    )


def _make_report(net_revenue=1245.30):
    return SimpleNamespace(
        date="2024-07-30",
        arbitrage_revenue_yuan=1753.46,
        dr_subsidy_yuan=0.0,
        degradation_cost_yuan=508.16,
        net_revenue_yuan=net_revenue,
        forecast_mape=3.14,
        alerts=["测试告警"],
    )


def _make_baseline():
    return SimpleNamespace(
        net_revenue_yuan=626.48,
        arbitrage_revenue_yuan=756.53,
        degradation_cost_yuan=130.04,
    )


def _make_digest(**kw) -> DecisionDigest:
    report = _make_report()
    sched = _make_schedule()
    d = build_digest(report, sched, _make_price(), baseline=_make_baseline())
    for k, v in kw.items():
        setattr(d, k, v)
    return d


# ========== 1. 窗口提取 ==========
def test_summarize_windows_extracts_charge_and_discharge():
    chg, dis = summarize_windows(_make_power(), _make_price())
    assert len(chg) == 1 and len(dis) == 1
    assert "充电" in chg[0] and "00:00-06:00" in chg[0]
    assert "放电" in dis[0] and "18:00-22:00" in dis[0]
    # 窗口描述里要带上均价，供 LLM 解释"为什么这个时段"
    assert "0.32元/kWh" in chg[0]
    assert "1.05元/kWh" in dis[0]


def test_summarize_windows_ignores_idle():
    chg, dis = summarize_windows(np.zeros(96), np.ones(96) * 0.65)
    assert chg == [] and dis == []


# ========== 2. 事实摘要 ==========
def test_digest_fields_are_consistent_with_inputs():
    d = _make_digest()
    assert d.net_revenue_yuan == pytest.approx(1245.30)
    assert d.arbitrage_revenue_yuan == pytest.approx(1753.46)
    assert d.max_temp_c == pytest.approx(44.0, abs=0.1)
    assert d.temp_headroom_c == pytest.approx(11.0, abs=0.1)
    assert d.energy_balance_error_kwh == 0.0
    assert d.soc_violation_steps == 0


def test_digest_improvement_is_steady_state_vs_baseline():
    d = _make_digest()
    expected = (1245.30 - 626.48) / 626.48 * 100
    assert d.improvement_pct == pytest.approx(expected, abs=0.01)
    assert d.baseline_net_yuan == pytest.approx(626.48)


def test_improvement_excludes_dr_subsidy_to_keep_same_baseline():
    """
    基准策略不参与需求响应。若把 DR 补贴算进"本方案"再去比基准，
    就会虚增提升幅度——这正是 稻草人基准的同类错误，必须拦住。
    """
    report = _make_report(net_revenue=1245.30 + 460.0)   # 含 DR 补贴
    report.dr_subsidy_yuan = 460.0
    d = build_digest(report, _make_schedule(), _make_price(), baseline=_make_baseline())

    assert d.net_without_dr_yuan == pytest.approx(1245.30)
    assert d.baseline_net_without_dr_yuan == pytest.approx(626.49, abs=0.01)
    # 提升幅度必须等于"不含DR"口径，而不是 (1245.3+460-626.48)/626.48
    assert d.improvement_pct == pytest.approx((1245.30 - 626.49) / 626.49 * 100, abs=0.01)
    dr_inflated = (1245.30 + 460.0 - 626.49) / 626.49 * 100
    assert d.improvement_pct < dr_inflated - 50


def test_digest_text_marks_same_caliber_comparison():
    text = _make_digest().to_text()
    assert "同口径" in text
    assert "均不含DR补贴" in text


def test_digest_annualized_is_365x():
    d = _make_digest()
    assert d.annualized_yuan == pytest.approx(1245.30 * 365, abs=1.0)


def test_digest_text_contains_all_key_facts():
    text = _make_digest().to_text()
    for token in ["1245.30", "1753.46", "44.0", "0.32元/kWh", "充电", "放电"]:
        assert token in text


# ========== 3. 防幻觉校验 ==========
def test_grounding_accepts_numbers_from_digest():
    d = _make_digest()
    answer = "日净收益 1245.30 元，最高温度 44.0℃，等效循环 1.2345 次。"
    rep = check_grounding(answer, d)
    assert rep.checked >= 3
    assert rep.grounded == rep.checked
    assert rep.unknown == []


def test_grounding_flags_fabricated_numbers():
    d = _make_digest()
    answer = "日净收益 1245.30 元，但如果放开约束可以做到 8888.00 元。"
    rep = check_grounding(answer, d)
    assert any("8888" in u for u in rep.unknown)
    assert rep.grounded >= 1
    assert rep.ratio < 1.0


def test_grounding_ignores_plain_counts():
    """'3 条原因' 这类计数词不该被当成待校验数字"""
    d = _make_digest()
    rep = check_grounding("下面给出 3 条原因，共 2 个建议。", d)
    assert rep.checked == 0
    assert rep.unknown == []


# ========== 4. 降级路径 ==========
def test_explainer_falls_back_to_rule_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    res = LLMExplainer(api_key="").explain(_make_digest())
    assert res.source == "rule"
    assert not res.used_llm
    assert "规则模板" in res.text
    assert "1245.30" in res.text


def test_explainer_degrades_when_client_raises():
    def boom(**kwargs):
        raise LLMError("HTTP 401: unauthorized")

    expl = LLMExplainer(api_key="sk-test", client=boom)
    res = expl.explain(_make_digest())
    assert res.source == "rule"
    assert "HTTP 401" in res.error
    assert res.used_llm is False


# ========== 5. LLM 路径 ==========
def test_explainer_uses_llm_and_marks_hallucination():
    def fake_client(**kwargs):
        # 模拟 LLM 编了一个摘要里没有的数字
        return "今日净收益 1245.30 元，若不限温度可达 9999.00 元。"

    expl = LLMExplainer(api_key="sk-test", client=fake_client)
    res = expl.explain(_make_digest())
    assert res.source == "llm"
    assert res.grounding is not None
    assert any("9999" in u for u in res.grounding.unknown)
    # 用户必须能看到警示，而不是默默放行
    assert "未能在事实摘要中匹配" in res.text


def test_explainer_passes_question_into_messages():
    captured = {}

    def fake_client(messages, **kwargs):
        captured["messages"] = messages
        return "净收益 1245.30 元。"

    expl = LLMExplainer(api_key="sk-test", client=fake_client)
    expl.explain(_make_digest(), "为什么14点不放电？")
    user_msg = captured["messages"][-1]["content"]
    assert "为什么14点不放电？" in user_msg
    assert "事实摘要" in user_msg
    # system prompt 必须明确禁止自行计算
    assert "只能使用摘要中出现的数字" in captured["messages"][0]["content"]


# ========== 6. 凭据解析 ==========
def test_resolve_credentials_prefers_deepseek_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    key, url, model = resolve_llm_credentials()
    assert key == "sk-deepseek"
    assert "deepseek" in url
    assert model == DEFAULT_MODEL


def test_resolve_credentials_explicit_args_win(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    key, url, model = resolve_llm_credentials(api_key="sk-explicit", base_url="http://x/v1", model="m1")
    assert key == "sk-explicit" and url == "http://x/v1" and model == "m1"


# ========== 7. HTTP 客户端（mock urllib，不联网） ==========
@contextmanager
def _fake_urlopen(payload):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _opener(req, timeout=None):
        assert req.get_header("Authorization") == "Bearer sk-test"
        assert json.loads(req.data.decode("utf-8"))["model"] == "deepseek-chat"
        return _Resp(json.dumps(payload).encode("utf-8"))

    with mock.patch("urllib.request.urlopen", _opener):
        yield


def test_call_chat_completions_parses_content():
    payload = {"choices": [{"message": {"content": "你好"}}]}
    with _fake_urlopen(payload):
        out = call_chat_completions(
            messages=[{"role": "user", "content": "hi"}],
            api_key="sk-test",
        )
    assert out == "你好"


def test_call_chat_completions_requires_key():
    with pytest.raises(LLMError):
        call_chat_completions(messages=[], api_key="")


def test_call_chat_completions_raises_on_bad_payload():
    with _fake_urlopen({"unexpected": True}):
        with pytest.raises(LLMError):
            call_chat_completions(messages=[], api_key="sk-test")
