"""全局中断标志，供所有模块检查 Ctrl+C 状态"""

import threading

_interrupted = threading.Event()


def set_interrupted():
    _interrupted.set()


def is_interrupted() -> bool:
    return _interrupted.is_set()
