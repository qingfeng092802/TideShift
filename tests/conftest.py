"""pytest 共享配置（🟠#24/#41）

此前 5 个测试文件各自 sys.path.insert 且测试依赖 CWD。
conftest.py 统一把项目根加入 sys.path，并确保任意 CWD 下可运行：
    cd / && pytest --rootdir=<项目根> <项目根>/tests
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 统一工作目录为项目根：消除"测试依赖 CWD"（🟠#41，data_loader 已改为
# __file__ 基准，但缓存/日志路径在测试断言中仍可能涉及相对路径）
os.chdir(PROJECT_ROOT)


@pytest.fixture()
def project_root() -> str:
    return PROJECT_ROOT
