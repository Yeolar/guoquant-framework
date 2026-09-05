"""
终端日志输出模块（基于 rich）：框架的统一日志出口。

提供统一的控制台与日志函数 ``info`` / ``error`` / ``warning`` /
``success``，并把后三者绑定为 ``console`` 的额外方法（见文末），供
调用方统一以 ``console.<level>()`` 风格输出。

被引用方：

- ``guoquant/commands/`` （CLI）；
- ``guoquant/common/fetch/`` （抓取模块）；
- ``guoquant/common/parallel.py`` （进度条）。

除 stdlib 与 rich 外，本模块不依赖任何行情库。
"""
import sys
import textwrap
from rich.console import Console


# 非终端环境（如重定向到文件/CI）下关闭 ANSI 颜色，避免输出乱码
is_terminal = sys.stdout.isatty()
console = Console(file=sys.stdout,
                  force_terminal=is_terminal,
                  no_color=not is_terminal)


def info(message, pretty=False):
    """打印普通信息文本到终端。

    Args:
        message (str): 待打印的信息文本。
        pretty (bool): 为 True 时先经 ``textwrap.dedent`` 去除公共缩进，
            默认 False。
    """
    if pretty:
        message = textwrap.dedent(message)
    console.print(message)


def error(message):
    """以红字加粗打印错误信息。

    Args:
        message (str): 待打印的错误信息文本。
    """
    console.print(message, style='red bold')


def warning(message):
    """以黄字加粗打印警告信息。

    Args:
        message (str): 待打印的警告信息文本。
    """
    console.print(message, style='yellow bold')


def success(message):
    """以绿色打印成功信息。

    Args:
        message (str): 待打印的成功信息文本。
    """
    console.print(message, style='green')


# 兼容 console.error(...) 调用（rich Console 本身没有这些方法，
# 绑定模块函数到实例属性；console.print(...) 等原生方法不受影响）。
console.error = error
console.warning = warning
console.success = success
