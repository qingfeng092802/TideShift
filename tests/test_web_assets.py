# -*- coding: utf-8 -*-
"""前端静态资源的接线约定。

这些用例不测业务，测的是"页面能不能被正确加载"这一层：
`?v=` 缓存位与版本脱钩时，改动对老用户是不可见的（真实踩过：加了新页面，
浏览器仍拿旧的 pages.js，侧栏里那个导航项根本不存在，而 git status 一切正常）；
资源文件被改名或漏提交时，症状是"页面半瘫且没有明显报错"。
"""
import re
from pathlib import Path

import pytest

from src import __version__

WEB = Path(__file__).resolve().parents[1] / "web"
INDEX = WEB / "index.html"

_ASSET_RE = re.compile(r'(?:href|src)="([^"]+?)"')


def _assets():
    """index.html 里引用的本地 css/js → (相对路径, 缓存位或 None)。"""
    out = []
    for m in _ASSET_RE.finditer(INDEX.read_text(encoding="utf-8")):
        raw = m.group(1)
        if raw.startswith(("http://", "https://", "//", "data:")):
            continue        # 外链不参与缓存位约定（本项目口径是无 CDN）
        path, _, query = raw.partition("?")
        if not path.endswith((".css", ".js")):
            continue
        version = None
        for kv in query.split("&"):
            if kv.startswith("v="):
                version = kv[2:]
        out.append((path, version))
    return out


def test_every_local_asset_is_versioned():
    """每个本地 css/js 都必须带 ?v=：漏一个就等于那个文件永远吃缓存。"""
    missing = [p for p, v in _assets() if not v]
    assert missing == [], f"这些资源没有缓存位：{missing}"


def test_cache_buster_matches_version():
    """缓存位必须等于 __version__，且只能有一个值。

    约定是"随发版手改"，所以它一旦和版本漂移，就说明有人改了前端却忘了升缓存位
    ——老用户看不到新页面，而仓库里什么都对。
    """
    seen = {v for _, v in _assets()}
    assert seen == {__version__}, (
        f"web/index.html 的 ?v= 是 {sorted(seen)}，而 __version__ 是 {__version__}；"
        f"改前端资源必须同步缓存位（随发版一起改）")


@pytest.mark.parametrize("path,version", _assets())
def test_referenced_asset_exists(path, version):
    """引用了不存在的文件时页面只会静默半瘫，这里提前让它红。"""
    assert (WEB / path).exists(), f"index.html 引用了缺失文件：{path}"


# ============================================================================
# 送达这一层：上面三条只保证仓库里的 ?v 是对的，但 ?v 能不能到浏览器，
# 取决于 index.html 本身每次是不是重新验证。StaticFiles 默认只发
# ETag/Last-Modified 而不发 Cache-Control ⇒ 浏览器走启发式缓存 ⇒
# "版本号进了、用户拿到的还是旧 index.html、于是继续请求旧 ?v"。
# 真实踩过两次（新增页面后侧栏没有该项 / 改了布局页面没变），
# 两次的共同症状都是"代码对了但用户看不见"，而 git status 一切正常。
# ============================================================================
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from backend import server
    return TestClient(server.app)


def test_html_entry_is_revalidated(client):
    """入口文档必须 no-cache，否则缓存位机制整体失效。"""
    for url in ("/", "/index.html"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert r.headers.get("cache-control") == "no-cache", \
            f"{url} 的 Cache-Control 是 {r.headers.get('cache-control')!r}；" \
            f"入口文档不重新验证的话，?v 升了也送不出去"
        assert 'href="css/app.css?v=' in r.text, "拿到的不是仓库里那份 index.html"


def test_versioned_assets_stay_cacheable(client):
    """css/js 不给 no-cache：它们靠 ?v 换 URL 失效，正是要留在缓存里的那部分。"""
    r = client.get(f"/css/app.css?v={__version__}")
    assert r.status_code == 200
    assert "no-cache" not in (r.headers.get("cache-control") or ""), \
        "给带缓存位的资源加 no-cache 等于让它每次都回源，?v 就没意义了"


def test_html_revalidate_costs_only_a_header_roundtrip(client):
    """no-cache 的代价是 304 而不是重传正文；顺带钉住 ETag 存在。"""
    etag = client.get("/").headers.get("etag")
    assert etag, "静态文件没有 ETag ⇒ no-cache 会每次全量重传入口文档"
    again = client.get("/", headers={"if-none-match": etag})
    assert again.status_code == 304
    assert again.content == b""


# ============================================================================
# 引擎参数单一事实来源：热管理页那张「SOC区间寿命衰减系数」柱状图此前把
# [3.0, 1.5, 1.0, 1.8] 硬编码在 pages.js 里，而 config.py 的 0.8-1.0 区间是 2.0
# —— 用户看到 1.8，优化器按 2.0 收费。同一张图还写死了 x 轴标签与 max=3.6。
# 下面两条一起守：后端必须下发这张表，前端必须不再自己抄一份。
# ============================================================================
PAGES_JS = WEB / "js" / "pages.js"


def test_bootstrap_ships_degradation_coefficients(client):
    """engine_config 必须带 SOC 区间衰减系数，且与引擎配置逐项一致。"""
    from backend import server
    from src.utils.config import CONFIG

    pwd = "web-assets-test-pwd"
    server.AUTH.set_password(pwd)
    tok = client.post("/api/auth/login",
                      json={"username": server.AUTH.username, "password": pwd}).json()["token"]
    boot = client.get("/api/bootstrap",
                      headers={"Authorization": f"Bearer {tok}"}).json()
    deg = (boot.get("engine_config") or {}).get("degradation") or {}
    assert deg.get("soc_coeff") == dict(CONFIG.battery.degradation_coeff), (
        f"engine_config.degradation.soc_coeff 是 {deg.get('soc_coeff')}，"
        f"而引擎实际按 {dict(CONFIG.battery.degradation_coeff)} 计费；"
        f"前端展示与优化器口径必须同源")
    unit = CONFIG.battery.total_battery_cost / (
        2.0 * CONFIG.battery.rated_capacity_kwh * CONFIG.battery.cycle_life)
    # 下发值 round 到 6 位小数，误差上限 5e-7；容差取 1e-6 覆盖取整本身
    assert deg.get("unit_cost_yuan_per_kwh") == pytest.approx(unit, abs=1e-6), (
        "engine_config 下发的衰减单位成本与 unit_cost 公式不符")


def test_frontend_does_not_hardcode_degradation_coefficients():
    """pages.js 不得再内联衰减系数数组——那是第二套口径的入口。

    只查"有没有把 1.8/2.0 这类系数写成数组"，不禁止出现数字本身
    （坐标轴上限、图例文案里出现数字是正常的）。
    """
    src = PAGES_JS.read_text(encoding="utf-8")
    assert "[3.0, 1.5, 1.0, 1.8]" not in src, (
        "pages.js 又出现了硬编码的衰减系数数组；应从 engineCfg().degradation.soc_coeff 读")
    assert "socDegradationCoeffs" in src, (
        "pages.js 里找不到 socDegradationCoeffs()：衰减系数图的取值函数被删了？")
    assert "degradation" in src and "soc_coeff" in src, (
        "pages.js 没有从 engine_config.degradation.soc_coeff 取值")


# ============================================================================
# 展示层的"第二套口径"清剿：engineCfg() 的回退对象、以及求解阶段时间线。
# 两处的共同问题是同一个数/同一段文案在语言边界两侧各存一份、手工对表。
# 回退副本更危险：它只在 bootstrap 不可用时才生效，平时测试看不到。
# ============================================================================
def _js_code_only(src: str) -> str:
    """剥掉 JS 注释，只留代码。

    "源码里不许再出现 X" 这类断言必须只看代码：注释里为了记录"此前是
    { capacity_kj_k: 15000, ... }"而原样引用旧值，是正常的、也是必要的，
    用裸 substring 检查会把这种注释判成违规（本项目踩过两次：
    cache_security 的 docstring 提到 pickle、以及这里的回退对象）。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)   # 块注释（含 JSDoc）
    src = re.sub(r"(?m)^\s*//.*$", "", src)           # 整行行注释
    return src


def test_pages_js_thermal_fallback_is_not_a_second_copy():
    """pages.js 不得内联热参数副本。

    `engineCfg()` 的注释写着"展示层禁止硬编码"，而它自己的回退对象里就抄了
    src/utils/config.py 的六个热参数（15000 / 0.001 / 18 / 95 / 45 / 55）。
    副本只在 bootstrap 失败时生效，所以任何"跑一遍页面"的测试都看不见它漂移。
    现在改成缺失即显示 "—"（thermalVal），这里钉住不再回退到具体数值。
    """
    src = _js_code_only(PAGES_JS.read_text(encoding="utf-8"))
    for needle in ("capacity_kj_k: 15000", "resistance_k_w: 0.001",
                   "internal_resistance_mohm: 18", "efficiency_pct: 95",
                   "temp_normal_max: 45", "temp_safe_max: 55"):
        assert needle not in src, (
            f"pages.js 的 engineCfg() 回退对象里又出现了硬编码热参数 {needle!r}；"
            f"后端不下发时应显示 '—'，不要再抄一份")
    assert "thermalVal" in src, "pages.js 缺少 thermalVal()：热参数的展示取值函数"
    # 回退对象本身不该带 thermal 的具体值（键存在但为空是允许的）
    assert re.search(r"thermal:\s*\{\s*\}", src), (
        "engineCfg() 的回退对象应把 thermal 留成空对象，具体值一律显示 '—'")


def test_bootstrap_ships_solve_stage_timeline(client):
    """求解阶段时间线必须由后端下发，且与 STAGE_MAP 同表。"""
    from backend import server

    pwd = "web-assets-test-pwd"
    server.AUTH.set_password(pwd)
    tok = client.post("/api/auth/login",
                      json={"username": server.AUTH.username, "password": pwd}).json()["token"]
    boot = client.get("/api/bootstrap",
                      headers={"Authorization": f"Bearer {tok}"}).json()
    stages = boot.get("solve_stages")
    assert isinstance(stages, list) and stages, "bootstrap 没下发 solve_stages"
    # 末项固定是 100% 的"全部完成"
    assert stages[-1]["percent"] == 100, f"时间线末项不是 100%：{stages[-1]}"
    # 百分比必须严格递增，否则 renderStages 的 findIndex 会指错阶段
    pcts = [s["percent"] for s in stages]
    assert pcts == sorted(set(pcts)), f"阶段百分比不是严格递增：{pcts}"
    # 与 STAGE_MAP 同源：每个节点的百分比都能在时间线里找到
    timeline = {s["percent"] for s in stages}
    for node, (_label, frac) in server.STAGE_MAP.items():
        assert frac in timeline, (
            f"STAGE_MAP[{node!r}] 的 {frac}% 不在下发的时间线 {sorted(timeline)} 里；"
            f"两张表已经分叉")
    # 每项都要有 label 与 desc，否则前端渲染出空条目
    for s in stages:
        assert s.get("label") and s.get("desc"), f"阶段项缺 label/desc：{s}"


def test_app_js_does_not_hardcode_solve_stages():
    """app.js 不得再内联阶段百分比与文案。"""
    src = _js_code_only((WEB / "js" / "app.js").read_text(encoding="utf-8"))
    for needle in ('[3, "准备调度引擎"]', '[25, "负荷预测（XGBoost）"]',
                   '"加载数据与电池参数，构建优化上下文"'):
        assert needle not in src, (
            f"app.js 又内联了求解阶段时间线片段 {needle!r}；应从 State.boot.solve_stages 读")
    assert "solveStages" in src, "app.js 缺少 solveStages()：阶段时间线的读取函数"


# ============================================================================
# 前端没有任何构建步骤，所以一个 JS 语法错误对 pytest 是完全不可见的：
# 页面白屏、控制台报 SyntaxError，而 300 多条 Python 测试全绿。
# 这里用 node --check 做纯语法解析（不执行、不联网、不需要 npm 依赖）。
# node 缺失时 skip 而不是 fail —— 项目本身不依赖 Node，测试环境未必有。
# ============================================================================
def _node_exe():
    import shutil
    return shutil.which("node")


@pytest.mark.skipif(_node_exe() is None, reason="未安装 node，跳过 JS 语法解析")
@pytest.mark.parametrize("name", ["app.js", "charts.js", "pages.js", "api.js"])
def test_js_parses(name):
    """每个前端脚本都必须能被 JS 引擎解析。"""
    import subprocess

    f = WEB / "js" / name
    assert f.exists(), f"前端脚本缺失：{f}"
    r = subprocess.run([_node_exe(), "--check", str(f)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, (
        f"{name} 有语法错误，页面会白屏而 Python 测试全绿：\n{r.stderr.strip()}")
