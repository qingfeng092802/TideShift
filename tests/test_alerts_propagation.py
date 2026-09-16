# -*- coding: utf-8 -*-
"""fix30：告警（alerts）透传链路专项测试。

背景：`web/js/pages.js` 的告警警示条读的是 `/api/page/dashboard` 的 `d.alerts`，
而该端点的返回体从未包含 `alerts` 字段 —— 渲染分支恒为假，负荷预测降级、
温度超限等告警在页面上永远看不到。此前的测试（含 test_langgraph.py 的双编排
一致性）从未断言过 `alerts`，所以这条链路是零覆盖的，缺陷得以长期存活。

本文件不触发 MILP：全部用构造对象 + monkeypatch，秒级可跑。
"""
import os
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

import server  # noqa: E402  backend/server.py
from src.agents.langgraph_coordinator import SchedulingState  # noqa: E402


# ---------------- 纯函数层：report_dict 的透传契约 ----------------

def _fake_report(alerts):
    return SimpleNamespace(
        net_revenue_yuan=1731.89, arbitrage_revenue_yuan=1702.79, dr_subsidy_yuan=533.77,
        degradation_cost_yuan=504.67, max_battery_temp_c=46.99, equivalent_cycles=1.8925,
        forecast_mape=3.12, solver_status="Optimal", time_limit_hit=False, mip_gap_pct=0.0,
        soc_violation_steps=0, energy_balance_error_kwh=0.0,
        explanation="x", explanation_source="rule", alerts=alerts,
    )


def test_report_dict_passes_alerts_through():
    """report_dict 必须原样透传 alerts，并统一转成 str 列表。"""
    d = server.report_dict(_fake_report(["电池温度超过正常上限（66.2℃），已触发降额"]))
    assert d["alerts"] == ["电池温度超过正常上限（66.2℃），已触发降额"]


def test_report_dict_alerts_is_list_not_none_when_absent():
    """report 无 alerts 属性时必须是空列表，不能是 None —— 否则前端 `d.alerts.length` 抛错。"""
    d = server.report_dict(_fake_report(None))
    assert isinstance(d["alerts"], list) and d["alerts"] == []


# ---------------- 端到端层：dashboard 端点必须下发 alerts ----------------

def _fake_viz(n=96):
    z = [0.0] * n
    return {
        "time_index": [f"2024-07-30T{i // 4:02d}:{(i % 4) * 15:02d}:00" for i in range(n)],
        "price": [0.32] * n, "soc": [0.5] * n,
        "charge_power": z[:], "discharge_power": z[:],
        "battery_temp": [30.0] * n, "load_forecast": [800.0] * n,
        "load_actual": None, "ambient_temp": [25.0] * n,
    }


@pytest.fixture()
def client_with_report(monkeypatch):
    """构造一个已求解的会话，可指定 report.alerts，绕开 MILP。

    同时绕过 JWT 中间件（本文件只验证 alerts 透传，不重复测认证；
    认证语义由 tests/test_server_api.py 覆盖）。返回的 payload 不带 ua_fp，
    中间件对无指纹 token 的兼容分支会直接放行。
    """
    def _make(alerts):
        base = SimpleNamespace(arbitrage_revenue_yuan=756.53, degradation_cost_yuan=130.04,
                               net_revenue_yuan=626.49, max_battery_temp_c=42.3,
                               equivalent_cycles=0.488)
        sess = SimpleNamespace(
            report=_fake_report(alerts), baseline=base, viz=_fake_viz(),
            params={"soc_min": 20, "soc_max": 90}, dr_signals=[], df=None,
        )
        monkeypatch.setattr(server, "_sess", lambda: sess)
        monkeypatch.setattr(server, "ensure_solved", lambda: True)
        monkeypatch.setattr(server.auth_mod, "jwt_verify", lambda tok: {"sub": "admin"})
        monkeypatch.setattr(server.AUTH, "must_change", lambda: False)
        return TestClient(server.app)
    return _make


def test_dashboard_exposes_alerts_field_with_content(client_with_report):
    """核心回归：dashboard 必须把 report.alerts 下发到顶层 alerts 字段。"""
    c = client_with_report(["负荷预测已降级：历史数据不足 11 天"])
    r = c.get("/api/page/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert "alerts" in body, "dashboard 返回体缺少 alerts 字段（前端告警条将永不显示）"
    assert body["alerts"] == ["负荷预测已降级：历史数据不足 11 天"]


def test_dashboard_alerts_key_present_even_when_empty(client_with_report):
    """无告警时字段也必须在，且为空数组（前端按 d.alerts && d.alerts.length 判断）。"""
    c = client_with_report([])
    body = c.get("/api/page/dashboard").json()
    assert "alerts" in body
    assert body["alerts"] == []


# ---------------- 图状态层：多节点告警必须累积而非覆盖 ----------------

def test_alerts_channel_uses_add_reducer():
    """alerts 用 operator.add 累积：负荷预测节点与储能节点各自产生的告警都要保留。"""
    import operator
    ann = SchedulingState.__annotations__["alerts"]
    assert getattr(ann, "__metadata__", None) == (operator.add,), \
        "alerts 通道必须使用 operator.add 累积语义，否则后一节点会覆盖前一节点的告警"
