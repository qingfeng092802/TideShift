"""运行追溯（trace）层的测试。

trace 是"跑坏了怎么查"的答案，所以它自己必须比被它记录的东西更可信：
这里重点测三件容易出事的地方 —— 线程隔离、脱敏口径、装饰器不改变签名。
"""
import inspect
import json
import re
import threading
from pathlib import Path

import pytest

from src.utils import trace


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """每个用例独立的 trace 目录与环形缓冲，避免用例之间互相看见。"""
    monkeypatch.setenv("ENERGY_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.delenv("ENERGY_TRACE_LEVEL", raising=False)
    monkeypatch.delenv("ENERGY_TRACE_MAX", raising=False)
    trace.reset()
    yield
    trace.reset()


def _jsonl_lines():
    d = trace.trace_dir()
    out = []
    for f in sorted(d.glob("*.jsonl")):
        out += [json.loads(line) for line in
                f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return out


# ---------- 基本形状 ----------

def test_run_collects_nested_spans():
    with trace.run("demo", date="2024-07-30") as rt:
        with trace.span("step_a"):
            pass
        with trace.span("step_b", ok=True):
            pass
    assert rt is not None
    summary = rt.summary()
    assert summary["kind"] == "demo" and summary["n_spans"] == 2
    assert summary["status"] == "ok"
    assert summary["attrs"]["date"] == "2024-07-30"
    assert summary["duration_ms"] >= 0


def test_nested_run_reuses_outer_run():
    with trace.run("outer") as outer:
        with trace.run("inner") as inner:
            with trace.span("work"):
                pass
    assert inner is outer, "嵌套必须并入最外层，否则一次对话被切成两条记录"
    assert [s.name for s in outer.spans] == ["work"]


def test_span_without_run_is_free_and_silent():
    with trace.span("orphan") as sp:
        assert sp is None
    assert trace.recent() == []


def test_error_status_is_recorded_and_exception_propagates():
    with pytest.raises(RuntimeError):
        with trace.run("boom") as rt:
            with trace.span("explodes"):
                raise RuntimeError("x")
    assert rt.status == "error:RuntimeError"
    detail = rt.detail()
    assert detail["spans"][0]["status"] == "error:RuntimeError"
    assert detail["errors"] == 1


# ---------- 脱敏口径 ----------

def test_basic_level_does_not_write_message_text():
    question = "用户自己的负荷数据 3005.5 kWh"
    with trace.run("chat"):
        with trace.span("ask", question=question):
            pass
    detail = trace.find(trace.recent()[0]["run_id"]).detail()
    attrs = detail["spans"][0]["attrs"]
    assert attrs == {"question_len": len(question)}, "默认级别不得把用户内容落盘"
    assert question not in json.dumps(detail, ensure_ascii=False)


def test_full_level_keeps_text_but_caps_length(monkeypatch):
    monkeypatch.setenv("ENERGY_TRACE_LEVEL", "full")
    with trace.run("chat"):
        with trace.span("ask", question="甲" * 5000, count=3):
            pass
    a = trace.find(trace.recent()[0]["run_id"]).detail()["spans"][0]["attrs"]
    assert len(a["question"]) == 2000 and a["count"] == 3


def test_lists_and_dicts_are_summarised_not_dumped():
    with trace.run("solve"):
        with trace.span("params", keys={"soc_max": 0.9, "rated_power_kw": 1000},
                        series=list(range(96))):
            pass
    a = trace.find(trace.recent()[0]["run_id"]).detail()["spans"][0]["attrs"]
    assert a == {"keys_keys": ["rated_power_kw", "soc_max"], "series_n": 96}


# ---------- 落盘与环形 ----------

def test_run_is_appended_to_jsonl():
    with trace.run("solve", date="2024-07-30"):
        with trace.span("milp"):
            pass
    rows = _jsonl_lines()
    assert len(rows) == 1
    assert rows[0]["kind"] == "solve" and rows[0]["spans"][0]["name"] == "milp"
    with trace.run("chat"):
        pass
    assert len(_jsonl_lines()) == 2, "追加写，不覆盖历史"


def test_ring_buffer_is_bounded(monkeypatch):
    monkeypatch.setenv("ENERGY_TRACE_MAX", "3")
    for i in range(10):
        with trace.run("tick"):
            pass
    assert len(trace.recent(limit=50)) == 3
    assert trace.recent()[0]["kind"] == "tick"


def test_unreadable_trace_dir_does_not_break_main_flow(monkeypatch, tmp_path):
    """盘不可用（只读、权限、磁盘满）绝不能把求解或对话主流程拖挂。

    造一个"父路径是文件"的目录：mkdir 必然失败，比依赖沙箱/权限更可控。
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setenv("ENERGY_TRACE_DIR", str(blocker / "traces"))
    with trace.run("solve"):
        with trace.span("milp"):
            pass
    assert len(trace.recent()) == 1


def test_level_off_disables_everything(monkeypatch):
    monkeypatch.setenv("ENERGY_TRACE_LEVEL", "off")
    with trace.run("solve") as rt:
        assert rt is None
        with trace.span("milp") as sp:
            assert sp is None
    assert trace.recent() == []
    assert _jsonl_lines() == []


# ---------- 线程隔离 ----------

def test_concurrent_threads_do_not_share_one_run():
    """求解在后台线程跑（start_solve._worker）。若活动 run 是模块级全局，
    并发两个请求的事件会混进同一个 run，trace 就成了假证据。"""
    barrier = threading.Barrier(2)

    def work(tag):
        with trace.run(f"req-{tag}"):
            barrier.wait()          # 两个线程同时在 run 内
            with trace.span("do"):
                pass

    threads = [threading.Thread(target=work, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    kinds = sorted(r["kind"] for r in trace.recent(limit=50))
    assert kinds == ["req-a", "req-b"]
    for r in trace.recent(limit=50):
        assert r["n_spans"] == 1, "线程之间串了事件"


# ---------- 装饰器 ----------

def test_traced_decorator_preserves_signature():
    """FastAPI 靠签名注入请求模型。装饰器改签名 = 端点直接失效。"""
    @trace.traced("chat")
    def endpoint(message: str, limit: int = 3):
        return {"reply": message, "n": limit}

    assert list(inspect.signature(endpoint).parameters) == ["message", "limit"]
    out = endpoint("hi")
    assert out == {"reply": "hi", "n": 3}
    summary = trace.recent()[0]
    assert summary["kind"] == "chat" and summary["status"] == "ok"
    assert summary["attrs"]["reply_keys"] == ["n", "reply"]


def test_traced_decorator_marks_failure_and_reraises():
    @trace.traced("solve")
    def boom():
        raise ValueError("no data")

    with pytest.raises(ValueError):
        boom()
    assert trace.recent()[0]["status"] == "error:ValueError"


def test_traced_tolerates_non_dict_result():
    @trace.traced("export")
    def blob():
        return b"csv-bytes"

    assert blob() == b"csv-bytes"
    assert "reply_keys" not in trace.recent()[0]["attrs"]


# ---------- 后端接口 ----------

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "backend"))
    import server
    server.AUTH.set_password("test-password-123")
    return TestClient(server.app)


def _headers(client):
    r = client.post("/api/auth/login", json={"username": "admin",
                                             "password": "test-password-123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_traces_endpoints_require_auth(client):
    assert client.get("/api/traces").status_code == 401
    assert client.get("/api/traces/whatever").status_code == 401


def test_traces_list_is_readable(client):
    h = _headers(client)
    with trace.run("manual", note="接口自检"):
        with trace.span("work"):
            pass
    r = client.get("/api/traces", headers=h)
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert runs and runs[0]["kind"] == "manual"
    rid = runs[0]["run_id"]
    detail = client.get(f"/api/traces/{rid}", headers=h)
    assert detail.status_code == 200
    assert detail.json()["spans"][0]["name"] == "work"


def test_traces_list_reports_its_own_caliber(client, monkeypatch):
    """追溯页要按接口给的口径自我解释（为什么看不到原文、为什么只有最近 N 次），
    所以级别与容量必须随环境变量走；同时绝不把本机目录路径回传给浏览器。"""
    monkeypatch.setenv("ENERGY_TRACE_LEVEL", "full")
    monkeypatch.setenv("ENERGY_TRACE_MAX", "7")
    with trace.run("manual"):
        pass
    meta = client.get("/api/traces", headers=_headers(client)).json()["meta"]
    assert set(meta) == {"level", "capacity", "kept"}
    assert meta["level"] == "full" and meta["capacity"] == 7
    assert meta["kept"] >= 1
    assert "dir" not in meta and str(trace.trace_dir()) not in json.dumps(meta, ensure_ascii=False)


def test_broken_caliber_env_falls_back(monkeypatch):
    """口径字段是用户可写的 env，写坏了要有默认值，不能让页面拿到 None 或 0。"""
    monkeypatch.setenv("ENERGY_TRACE_LEVEL", "verbose")
    monkeypatch.setenv("ENERGY_TRACE_MAX", "abc")
    st = trace.status()
    assert st["level"] == "basic" and st["capacity"] == 64


def test_nav_pages_are_wired_end_to_end():
    """加了导航项却忘了注册渲染器 / 忘了放 <section>，是"点一下才炸"的那类错误，
    所以在不启动浏览器的前提下先把三处对齐钉住。"""
    root = Path(__file__).resolve().parents[1]
    nav = re.findall(r'\{ id: "([a-z-]+)", label:',
                     (root / "web/js/app.js").read_text(encoding="utf-8"))
    sections = re.findall(r'id="page-([a-z-]+)"',
                          (root / "web/index.html").read_text(encoding="utf-8"))
    renderers = re.findall(r"^  ([a-z-]+): \{ render: ",
                           (root / "web/js/pages.js").read_text(encoding="utf-8"), re.M)
    assert nav and set(nav) == set(sections) == set(renderers), (
        f"NAV={nav} sections={sections} RENDERERS={renderers}")
    assert "traces" in nav


def test_unknown_run_id_is_404_not_path_lookup(client):
    """run_id 只当查询键用，绝不拼进文件路径 —— 否则就是现成的目录穿越入口。"""
    h = _headers(client)
    for probe in ("../../etc/passwd", "..\\..\\windows\\win.ini", "nope"):
        r = client.get(f"/api/traces/{probe}", headers=h)
        assert r.status_code in (404, 400), (probe, r.status_code)
    assert not (trace.trace_dir() / "nope").exists()
