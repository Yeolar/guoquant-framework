"""
QMT（迅投）行情上下文——xtdata 连接管理，以及 Futu/QMT 代码格式互转。

职责：模块内提供两个工具——

- ``open_quote_context``：QMT 模式下由 fetch/__init__.py 再导出，供各
  commands/``fetch_*.py`` 使用（与 futu provider 同名同接口，见
  futuapi/context.py）；
- ``to_qmt_code``：Futu 格式（SZ.000001）转 QMT 格式（000001.SZ）的
  纯函数，被 fetch/qmtapi/stockquote.py 与 guoquant.common.trade 的
  data.py / executor.py、commands/auto_trade.py / watch.py 等引用。

数据落盘：本模块不落盘，只负责连接与代码转换；落盘见各抓取函数。

约定（guoziapp 模板改造）：xtquant 为可选依赖（随 QMT 客户端分发），
延迟到 open_quote_context 被调用时才 import；to_qmt_code 等纯函数不
依赖 xtquant。open_quote_context 连接的是本机正在运行的 QMT 客户端
（xtdata.connect() 无需手动指定地址），连接失败/客户端未启动时在调用
处抛异常。
"""
import logging
import re
from contextlib import contextmanager


# Futu 代码格式：市场(SZ/SH) + '.' + 6 位数字，如 'SZ.000001'
_FUTU_CODE_RE = re.compile(r'^(SZ|SH)\.[0-9]{6}$')


def to_qmt_code(futu_code):
    """将 Futu 代码格式（SZ.000001）转为 QMT 格式（000001.SZ）。

    转换口径：仅对匹配模块级 _FUTU_CODE_RE（市场 SZ/SH + '.' + 6 位
    数字）的代码做转换；已是 QMT 格式或其他格式的代码原样返回，
    可安全用于混合输入。

    Args:
        futu_code (str): 待转换的代码，如 ``SZ.000001``。

    Returns:
        str: QMT 格式代码（如 ``000001.SZ``）；不匹配 Futu 格式时
        返回原样输入。
    """
    if not _FUTU_CODE_RE.match(futu_code):
        return futu_code
    market, code = futu_code.split('.')
    return f'{code}.{market}'


@contextmanager
def open_quote_context(loglevel=logging.WARNING):
    """打开连接到本机 QMT 客户端的 xtdata 行情上下文。

    调用时（进入 with 块）才 import xtquant 并执行 xtdata.connect()
    （连接本机正在运行的 QMT 客户端，无需手动指定地址）；随后把 xtquant
    日志（logger 名 xtquant）级别设为 loglevel；退出 with 块时
    （finally 分支）自动 disconnect()。连接失败或客户端未启动时，
    异常在调用处（本函数内 xtdata.connect() 或 import 处）抛出。

    Args:
        loglevel (int, optional): xtquant 日志级别；默认 logging.WARNING，
            用于屏蔽 INFO 级刷屏日志。

    Yields:
        module: xtquant.xtdata 模块对象，供抓取函数调用行情接口；退出
        with 块后已 disconnect()，不可再使用。
    """
    from xtquant import xtdata
    logging.getLogger('xtquant').setLevel(loglevel)
    xtdata.connect()
    try:
        yield xtdata
    finally:
        xtdata.disconnect()
