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
