"""pytest 共享配置（🟠#24/#41）

此前 5 个测试文件各自 sys.path.insert 且测试依赖 CWD。
conftest.py 统一把项目根加入 sys.path，并确保任意 CWD 下可运行：
    cd / && pytest --rootdir=<项目根> <项目根>/tests
"""
import os
import shutil
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 统一工作目录为项目根：消除"测试依赖 CWD"（🟠#41，data_loader 已改为
# __file__ 基准，但缓存/日志路径在测试断言中仍可能涉及相对路径）
os.chdir(PROJECT_ROOT)


# ============================================================================
# 测试期状态隔离 —— 必须在任何测试模块导入 server 之前生效
# ----------------------------------------------------------------------------
# 背景（阻断级问题）：backend/server.py 在**导入时**就实例化 AuthStore，
# 而 AuthStore/_config_dir() 默认把 auth.json、.auth_secret、.api_secret 写到
# <项目根>/config/。测试一旦把这些文件写进仓库工作树，用户按 README 的
# 「装依赖 → 跑 pytest → 启动服务」路径操作时，服务启动会读到测试生成的随机
# 口令而跳过 _create_default()，于是 ADMIN_INITIAL_PASSWORD 被**静默忽略**，
# 任何口令都登录失败。把可变状态整体重定向到临时目录即可根治本类问题。
#
# conftest.py 先于全部测试模块被导入，因此在模块级设置环境变量是可靠的。
# ============================================================================
_TEST_STATE_DIR = tempfile.mkdtemp(prefix="tideshift-test-state-")
os.environ["ENERGY_CONFIG_DIR"] = _TEST_STATE_DIR
os.environ.setdefault("ENERGY_CACHE_SECRET_FILE",
                      os.path.join(_TEST_STATE_DIR, ".cache_secret"))


@pytest.fixture(scope="session", autouse=True)
def _isolated_state_dir():
    """会话结束后清理测试状态目录（清理失败不影响测试结论）。"""
    yield _TEST_STATE_DIR
    shutil.rmtree(_TEST_STATE_DIR, ignore_errors=True)


@pytest.fixture()
def project_root() -> str:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def isolated_state_dir() -> str:
    """供断言使用：确认测试状态确实落在临时目录而非仓库工作树。"""
    return _TEST_STATE_DIR
