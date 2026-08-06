"""
结构化日志 — 双输出（控制台 + 文件）
"""

import logging
import os
import sys
from datetime import datetime

_LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
_LOG_FILE: str | None = None
_log_initialized = False


def _init():
    global _log_initialized, _LOG_FILE
    if _log_initialized:
        return

    os.makedirs(_LOG_DIR, exist_ok=True)
    _LOG_FILE = os.path.join(_LOG_DIR, f"live-{datetime.now().strftime('%Y%m%d')}.log")

    root = logging.getLogger()  # 根 logger，所有模块自动继承
    root.setLevel(logging.DEBUG)

    # 控制台 handler — INFO 以上
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(message)s"  # 控制台简洁
    ))
    root.addHandler(console)

    # 文件 handler — DEBUG 以上，带时间戳
    file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    ))
    root.addHandler(file_handler)

    _log_initialized = True


def get_logger(name: str = "jianing") -> logging.Logger:
    """获取 logger。首次调用自动初始化。"""
    _init()
    return logging.getLogger(name)


def log_file_path() -> str:
    """返回当前日志文件路径"""
    _init()
    return _LOG_FILE or ""
