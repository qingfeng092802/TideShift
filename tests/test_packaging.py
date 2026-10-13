"""
打包元数据一致性测试（P2.2）

被测对象：`pyproject.toml`。

为什么需要这个文件：新增 pyproject.toml 后，运行依赖就有了**两处声明**
（requirements.txt 面向使用者，pyproject.toml 面向 `pip install -e .`）。
两处漂移不会让任何测试变红——打包元数据不参与运行——但会让"按 README 装"
与"按 pyproject 装"得到两套环境。这里的断言把"不许漂移"变成机械检查。

另钉住三条容易踩的事实：
1. 版本号只有一个来源（`src/__init__.py` 的 `__version__`），pyproject 用
   `dynamic` 引用它，不抄第二个数字。
2. 包发现范围不含 tests/（测试不是发行物）。
3. 项目**明确不支持** `pip install .`：web/ 与 data/ 是按 `__file__` 相对项目根
   解析的运行期资源，装进 site-packages 后服务起不来。README 与 pyproject 都
   必须如实说明，不能假装能装。
"""
import re
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
REQUIREMENTS_DEV = PROJECT_ROOT / "requirements-dev.txt"


def _load_pyproject() -> dict:
    assert PYPROJECT.exists(), f"缺少 {PYPROJECT}"
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)


def _parse_req_line(line: str):
    """把 requirements*.txt 的一行解析成 (规范包名, 版本区间字符串)。

    只处理 `包名>=x,<y` 形式（本仓的固定写法）；注释行、空行、`-r` 引用返回 None。
    """
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return None
    m = re.match(r"^([A-Za-z0-9._-]+)\s*(\S*)$", line)
    if not m:
        return None
    name, spec = m.group(1), m.group(2)
    # PEP 503 规范化：下划线/点/连字符等价，大小写不敏感
    return re.sub(r"[-_.]+", "-", name).lower(), spec


def _parse_requirements(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_req_line(line)
        if parsed:
            out[parsed[0]] = parsed[1]
    return out


def _parse_pep508(spec: str):
    """把 `numpy>=2.5.1,<3.0` 解析成 (规范名, 区间)。"""
    m = re.match(r"^([A-Za-z0-9._-]+)\s*(.*)$", spec.strip())
    assert m, f"无法解析依赖串：{spec!r}"
    name = re.sub(r"[-_.]+", "-", m.group(1)).lower()
    return name, m.group(2).strip()


def test_version_is_single_source():
    """版本号只有 src/__init__.py 一处来源，pyproject 必须 dynamic 引用它。"""
    data = _load_pyproject()
    assert "version" in data["project"]["dynamic"], (
        "pyproject.toml 应通过 dynamic 引用 src.__version__，"
        "而不是再写一个版本号（README「代码约定」钉的唯一事实来源）")
    assert "version" not in data["project"], "不应同时写死 version 与 dynamic"
    attr = data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "src.__version__", f"版本来源应为 src.__version__，实为 {attr}"

    # 实际能被解析出来，且与 src/__init__.py 一致
    import src
    assert re.match(r"^\d+\.\d+\.\d+$", src.__version__), src.__version__


def test_pyproject_runtime_deps_match_requirements():
    """pyproject 的运行依赖必须与 requirements.txt 逐条一致（包名 + 版本区间）。"""
    py_deps = {}
    for spec in _load_pyproject()["project"]["dependencies"]:
        name, rng = _parse_pep508(spec)
        py_deps[name] = rng
    txt_deps = _parse_requirements(REQUIREMENTS)

    missing = set(txt_deps) - set(py_deps)
    extra = set(py_deps) - set(txt_deps)
    assert not missing, f"requirements.txt 有但 pyproject.toml 缺：{sorted(missing)}"
    assert not extra, f"pyproject.toml 有但 requirements.txt 缺：{sorted(extra)}"

    mismatched = {n: (txt_deps[n], py_deps[n]) for n in txt_deps if txt_deps[n] != py_deps[n]}
    assert not mismatched, (
        "以下包的版本区间两处不一致（requirements.txt, pyproject.toml）："
        f"{mismatched}")


def test_pyproject_dev_deps_match_requirements_dev():
    """dev 依赖同样不许漂移（ruff / mypy 已进 requirements-dev.txt）。"""
    py_dev = {}
    for spec in _load_pyproject()["project"]["optional-dependencies"]["dev"]:
        name, rng = _parse_pep508(spec)
        py_dev[name] = rng
    txt_dev = _parse_requirements(REQUIREMENTS_DEV)

    for name, rng in py_dev.items():
        assert name in txt_dev, f"pyproject 的 dev 依赖 {name} 未出现在 requirements-dev.txt"
        assert txt_dev[name] == rng, (
            f"{name} 版本区间不一致：requirements-dev.txt={txt_dev[name]!r}，"
            f"pyproject={rng!r}")


def test_lint_tools_are_declared_for_ci():
    """CI 的 lint job 装的 ruff/mypy 必须也在 requirements-dev.txt 里声明。

    否则会出现"CI 能跑、开发者本机按 README 装完却没有这个命令"。
    """
    txt_dev = _parse_requirements(REQUIREMENTS_DEV)
    for tool in ("ruff", "mypy"):
        assert tool in txt_dev, (
            f"{tool} 未在 requirements-dev.txt 声明，但 CI 的 lint job 会用到它")
    py_dev = {_parse_pep508(s)[0] for s in
              _load_pyproject()["project"]["optional-dependencies"]["dev"]}
    for tool in ("ruff", "mypy"):
        assert tool in py_dev, f"{tool} 未在 pyproject 的 dev extra 声明"


def test_package_discovery_excludes_tests():
    """包发现范围只含 src/backend/evals，不含 tests/。"""
    include = _load_pyproject()["tool"]["setuptools"]["packages"]["find"]["include"]
    assert include == ["src*", "backend*", "evals*"], include
    assert not any("tests" in pat for pat in include), "测试不是发行物的一部分"


def test_console_scripts_point_to_real_functions():
    """[project.scripts] 指向的入口必须真实存在且可调用。

    加这个断言的直接原因：`backend/server.py` 原先只在
    `if __name__ == "__main__"` 里启动，没有任何可被 import 的入口函数，
    装了 `pip install -e .` 得到的 `tideshift` 命令必然是坏的。
    """
    import importlib
    scripts = _load_pyproject()["project"]["scripts"]
    assert scripts, "应声明 tideshift / tideshift-manage 两个入口"
    for cmd, target in scripts.items():
        mod_name, _, fn_name = target.partition(":")
        assert fn_name, f"{cmd} 的入口 {target!r} 缺少 :函数名"
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, fn_name, None)
        assert callable(fn), f"{cmd} 指向的 {target} 不存在或不可调用"


def test_requires_python_matches_python_version_file():
    """requires-python 与 .python-version 口径一致（README 声明 3.13）。"""
    data = _load_pyproject()
    assert data["project"]["requires-python"] == ">=3.13", data["project"]["requires-python"]
    pv = (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert pv.startswith("3.13"), f".python-version={pv!r} 与 requires-python 不一致"


def test_readme_documents_editable_install_limitation():
    """README 必须说明只支持 `pip install -e .`、不支持 `pip install .`。

    这条不是文风要求：web/ 与 data/ 按 __file__ 相对项目根解析，非 editable
    安装后服务起不来。README 不写，使用者就会按常规习惯 `pip install .`
    然后遇到一个"装成功了但起不来"的问题。
    """
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "pip install -e ." in readme, "README 未给出可编辑安装命令"
    assert "pyproject.toml" in readme, "README 未提到 pyproject.toml"


def test_pytest_config_has_single_owner():
    """pytest 配置只放 pytest.ini，不在 pyproject.toml 里重复。

    pytest 的优先级是 pytest.ini > pyproject.toml，两处都写会让"到底哪个生效"
    变成需要查文档的问题。pytest.ini 带中文注释说明 slow 标记分工，是既有所有者。
    """
    data = _load_pyproject()
    assert "pytest" not in data.get("tool", {}), (
        "pyproject.toml 不应再写 [tool.pytest.ini_options]："
        "pytest.ini 是唯一所有者")


def test_ruff_config_is_scoped_and_documented():
    """ruff 配置存在且行长上限与实测口径相符。"""
    ruff = _load_pyproject()["tool"]["ruff"]
    assert ruff["line-length"] == 120, (
        "行长上限 120：本仓 99.5 分位 104 字符，120 是能守住的上限")
    assert ruff["target-version"] == "py313"
    assert "web/vendor" in ruff["extend-exclude"], "第三方 vendor 目录应排除"
    selected = _load_pyproject()["tool"]["ruff"]["lint"]["select"]
    for rule in ("E501", "F", "I", "B"):
        assert rule in selected, f"ruff 规则集缺少 {rule}"


def test_mypy_scope_is_src():
    """mypy 作用域收在 src/，并开启 no_implicit_optional。"""
    mypy_cfg = _load_pyproject()["tool"]["mypy"]
    assert mypy_cfg["files"] == ["src"], mypy_cfg["files"]
    assert mypy_cfg["no_implicit_optional"] is True, (
        "开启 no_implicit_optional：`x: int = None` 是隐式 Optional，"
        "在 3.13 + 新 mypy 下是错误而不是警告")
    assert mypy_cfg["ignore_missing_imports"] is True


def test_precommit_config_lists_ruff_and_mypy():
    """pre-commit 配置存在，且钩子与 CI 的 lint job 覆盖同一套工具。"""
    import yaml
    cfg_path = PROJECT_ROOT / ".pre-commit-config.yaml"
    assert cfg_path.exists(), "缺少 .pre-commit-config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    hook_ids = {h["id"] for repo in cfg["repos"] for h in repo["hooks"]}
    for required in ("ruff", "mypy"):
        assert required in hook_ids, f"pre-commit 缺少 {required} 钩子"
    # mypy 钩子不能吃 pre-commit 传的文件名，否则会绕过 pyproject 的 files=["src"]
    for repo in cfg["repos"]:
        for h in repo["hooks"]:
            if h["id"] == "mypy":
                assert h.get("pass_filenames") is False, (
                    "mypy 钩子必须 pass_filenames: false，"
                    "否则作用域变成'本次改动的文件'而非 pyproject 里的 src/")


def test_ci_has_lint_job():
    """CI 必须有 lint job，且判定口径与本地一致（ruff check / mypy 裸命令）。"""
    import yaml
    ci = yaml.safe_load((PROJECT_ROOT / ".github" / "workflows" / "ci.yml")
                        .read_text(encoding="utf-8"))
    assert "lint" in ci["jobs"], f"CI 缺少 lint job，现有：{sorted(ci['jobs'])}"
    steps = ci["jobs"]["lint"]["steps"]
    runs = [s["run"] for s in steps if "run" in s]
    assert any("ruff check" in r for r in runs), f"lint job 未跑 ruff check：{runs}"
    assert any(r.strip() == "mypy" for r in runs), (
        f"lint job 应跑裸 `mypy`（读 pyproject 的 files=src），实为：{runs}")
    # 不应在 CI 里 --fix：CI 只判定
    assert not any("--fix" in r for r in runs), "CI 不应带 --fix，那是本地钩子的事"


def test_editable_install_limitation_is_documented():
    """pyproject 文件头必须写明只支持 -e 安装的原因。"""
    text = PYPROJECT.read_text(encoding="utf-8")
    assert "pip install -e ." in text
    assert "pip install ." in text, "应明确写出不支持哪种装法"
    assert "site-packages" in text, "应说明非 editable 安装为何起不来"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
