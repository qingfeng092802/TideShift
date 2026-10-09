# -*- coding: utf-8 -*-
"""`tools/capture_screenshots.py` 的静态守卫（fast 集，不起浏览器）。

截图这件事没法在 CI 里跑（要真浏览器 + 真求解 + 一次登录），但**它的三个失效模式
是可以在毫秒级钉住的**：

1. 一个文档工具偷偷引入第三方依赖 —— 那会直接违背"零新增依赖 / 完全离线可用"的口径，
   而 `requirements*.txt` 没同步时只有别人 clone 下来才炸；
2. 脚本里写的文件名与 README 引用的文件名漂移 —— 图改了名，README 里就是四个 404，
   而 `git status` 干干净净；
3. 采集尺寸变了 —— README 的排版会跳，且新旧图不同尺寸这件事没人会注意到。

失败即说明对应的那条约定被破坏，不是环境抖动。
"""
from __future__ import annotations

import ast
import re
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "capture_screenshots.py"
README_CN = ROOT / "README.md"
README_EN = ROOT / "README.en.md"
SHOT_DIR = ROOT / "docs" / "screenshots"

# 脚本负责产出的静态截图（GIF 由另一条尚未脚本化的链路负责，不在此列）
EXPECTED_PNGS = [
    "01-welcome.png",
    "02-dashboard-light.png",
    "03-scheduling.png",
    "04-thermal.png",
    "05-dashboard-dark.png",
]


def _imported_modules() -> set[str]:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    return mods


def test_capture_tool_imports_only_stdlib():
    """失败 = 这个文档工具开始吃第三方依赖，要么删掉它，要么正式进 requirements-dev.txt。"""
    import sys
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    assert stdlib, "Python 太老，没有 sys.stdlib_module_names（需 ≥3.10）"
    local = {"tools", "capture_screenshots"}
    extra = sorted(_imported_modules() - stdlib - local)
    assert not extra, f"capture_screenshots.py 引入了非标准库模块：{extra}"


def test_capture_tool_writes_exactly_the_readme_pngs():
    """脚本里 shot() 的文件名集合必须等于 README 引用的静态截图集合。"""
    written = set(re.findall(r'shot\(cdp,\s*out_dir,\s*"([^"]+\.png)"',
                             SCRIPT.read_text(encoding="utf-8")))
    assert written == set(EXPECTED_PNGS), f"脚本产出的图名漂移：{sorted(written)}"
    for md in (README_CN, README_EN):
        referenced = set(re.findall(r"docs/screenshots/([0-9][^)\s]*\.png)",
                                    md.read_text(encoding="utf-8")))
        assert referenced == written, f"{md.name} 引用的截图与脚本产出不一致：{sorted(referenced)}"


def test_readme_referenced_pngs_exist_at_capture_viewport():
    """README 引用的每张图都得真的在，且是脚本约定的 1680×1050（IHDR 直接读，不依赖 Pillow）。"""
    for name in EXPECTED_PNGS:
        p = SHOT_DIR / name
        assert p.exists(), f"{p.relative_to(ROOT)} 不存在，README 会挂出 404"
        head = p.read_bytes()[:33]
        assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{name} 不是合法 PNG"
        w, h = struct.unpack(">II", head[16:24])
        assert (w, h) == (1680, 1050), f"{name} 尺寸是 {w}×{h}，与采集脚本的视口约定不一致"
