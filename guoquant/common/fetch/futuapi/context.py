"""
富途（Futu）行情上下文——OpenQuoteContext 的连接管理与统一日志降噪。

职责：作为 fetch 的默认 provider（``.env`` 中 ``FETCH_PROVIDER`` 未配置
或为 futu 时），由 fetch/__init__.py 从本模块导出 open_quote_context；
commands/``fetch_*.py`` 通过 ``with open_quote_context() as ctx:`` 打开
连接，再把 ctx 传给本包（futuapi/）内各抓取函数使用，退出 with 块时
自动 close()，保证连接用完即关。

数据落盘：本模块不落盘，只负责连接；数据落盘由各抓取函数完成（总览见
fetch/__init__.py 模块 docstring）。

依赖约定：需要 futu-api（pip 包，提供 futu.OpenQuoteContext），且本机需
运行富途 OpenD 并监听默认端口 11111；依赖缺失或 OpenD 未启动时，分别在
模块导入或连接建立环节报错（导入失败由 fetch/__init__.py 捕获跳过）。
"""
import logging
from contextlib import contextmanager
from futu import OpenQuoteContext


@contextmanager
def open_quote_context(loglevel=logging.WARNING):
    """打开一个连接到本机 OpenD（127.0.0.1:11111）的行情上下文。

    进入 with 块时把富途 SDK 日志（logger 名 FTConsoleLog）级别设为
    loglevel，再创建连接；退出 with 块时（finally 分支）自动 close()。

    Args:
        loglevel (int, optional): 富途 SDK 控制台日志级别；默认
            logging.WARNING，用于屏蔽刷屏的 INFO 级连接/推送日志。

    Yields:
        futu.OpenQuoteContext: 行情上下文实例，供抓取函数调用行情接口；
        退出 with 块后连接已关闭，实例不可再使用。
    """
    logging.getLogger('FTConsoleLog').setLevel(loglevel)
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    try:
        yield ctx
    finally:
        ctx.close()
