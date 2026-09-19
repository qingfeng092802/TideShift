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
