# -*- coding: utf-8 -*-
"""统一日志

全仓此前 0 处 logging、40+ 处 print，求解异常只截断塞进内存 progress 从不落盘，
线上事故零可查证据。本模块提供进程级单例 logger：
- 控制台 + logs/app.log RotatingFileHandler（5MB × 3 备份）
- 供 Web 后端 / CLI / 求解回退路径统一使用
- 用法：from src.utils.logger import get_logger; log = get_logger(__name__)
"""
import logging
import os
from logging.handlers import RotatingFileHandler

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_configured = False


def _setup() -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger("energy_dispatch")
    root.setLevel(logging.INFO)
    # 避免重复挂 handler（模块被多次导入时）
    if root.handlers:
        _configured = True
        return
    fmt = logging.Formatter(_LOG_FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        # 日志目录以项目根（本文件 parents[2]）为基准，不依赖 CWD
        log_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "logs")
        os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(log_dir, "app.log"),
                                 maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass  # 日志盘不可用时退化为仅控制台
    _configured = True


def get_logger(name: str = "energy_dispatch") -> logging.Logger:
    """获取项目命名空间下的 logger（自动初始化 handler）。"""
    _setup()
    if name.startswith("energy_dispatch"):
        return logging.getLogger(name)
    return logging.getLogger(f"energy_dispatch.{name}")
