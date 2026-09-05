"""
guoquant 通用工具模块（路径 / 日期 / 限速，无行情库依赖）。

提供：

- ``BASE_DIR`` / ``outp``：项目根目录常量与 ``out/`` 输出路径构造
  （按需建目录）；
- ``todate``：``'YYMMDD'`` → ``date`` 的日期格式转换入口；
- ``keep_sec`` / ``keep_one_sec`` / ``keep_one_minute``：保证代码块耗时
  不低于指定时长的限速上下文管理器（行情接口限速用）。

被 ``commands/``、``common/fetch/`` （抓取限速）、``common/data_path.py``
（``BASE_DIR``）等模块引用；不依赖任何行情库。
"""
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

# 项目根目录（guoquant 包所在目录；utils.py 位于 guoquant/common/ 下，
# 因此上溯三级：common → guoquant → 项目根）
BASE_DIR = Path(__file__).resolve().parent.parent.parent


def outp(*args, is_dir=False):
    """构造 ``out/`` 下的输出路径（按需创建父目录）。

    用法示例：

    .. code-block:: python

        outp('260101/fetched_data')  # -> <项目根>/out/260101/fetched_data

    Args:
        *args (str): 依次拼接在 ``out/`` 后的路径片段。
        is_dir (bool): 为 True 时创建完整目录（含末级）；为 False 时仅创建
            父目录（末级通常由后续写文件创建），默认 False。

    Returns:
        pathlib.Path: ``<BASE_DIR>/out/`` 拼接 ``*args`` 后的完整路径。
    """
    out = BASE_DIR / 'out'
    for arg in args:
        out /= arg
    if is_dir:
        out.mkdir(mode=0o755, parents=True, exist_ok=True)
    else:
        out.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    return out


@contextmanager
def keep_sec(sec=1):
    """限速上下文管理器：保证代码块执行时间不少于 ``sec`` 秒。

    执行耗时不足 ``sec`` 秒时在退出前 ``sleep`` 补齐，用于对行情接口
    限速（避免请求过频被风控）；逐股/逐批抓取的固定间隔特例见
    ``keep_one_sec`` / ``keep_one_minute``。

    Args:
        sec (int): 代码块最短执行秒数，默认 1。
    """
    t = time.time()
    try:
        yield
    finally:
        time.sleep(max(sec - (time.time() - t), 0))


@contextmanager
def keep_one_sec():
    """``keep_sec`` 特例：保证代码块耗时至少 1 秒（逐股抓取时的限速）。"""
    t = time.time()
    try:
        yield
    finally:
        time.sleep(max(1 - (time.time() - t), 0))


@contextmanager
def keep_one_minute():
    """``keep_sec`` 特例：保证代码块耗时至少 60 秒（批量 K 线订阅限速）。"""
    t = time.time()
    try:
        yield
    finally:
        time.sleep(max(60 - (time.time() - t), 0))


def todate(yymmdd):
    """把 ``'YYMMDD'`` 格式字符串转为 ``date`` 对象（自动补 ``'20'`` 前缀）。

    抓取数据按 ``'YYMMDD'`` 目录组织（如 ``'240101'``），与标准
    ``'YYYY-MM-DD'`` 表示不同；本函数是两套日期约定的唯一转换入口。

    Args:
        yymmdd (str): ``'YYMMDD'`` 格式的日期字符串，如 ``'240101'``。

    Returns:
        datetime.date: 对应 20YY-MM-DD 的日期对象（视为 2000 年后）。
    """
    return date.fromisoformat('20' + yymmdd)
