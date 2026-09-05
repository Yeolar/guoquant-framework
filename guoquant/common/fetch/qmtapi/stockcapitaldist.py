"""
筹码分布抓取（QMT）——不支持的占位实现。

职责：QMT xtdata 无个股筹码分布（成本分布）接口，本模块保留与 futu
provider 版（futuapi/stockcapitaldist.py）一致的函数签名
（progress/ctx/codes/outputdir），使 provider 切换时 fetch 命令无需
分支。本实现不抓取任何数据、不落盘，调用即报错（详见函数 docstring）。
"""


def fetch_stock_capital_dist(progress, ctx, codes, outputdir):
    """占位实现：与 futu 版同名同签名，QMT xtdata 不支持筹码分布。

    已知缺陷：函数体引用 console 打印错误提示，但本模块未导入 console
    （缺少 from guoquant.common.log import console），实际调用会先抛
    NameError: name 'console' is not defined，而不是打印出这里的提示；
    即使补上导入，也只是打印提示后返回，不抓取、不落盘。保留同名同
    签名仅为让 fetch 命令按 provider 统一调用（如 fetch_stockdata /
    fetch_stockcapitaldist 命令的 dist 分支）。

    Args:
        progress: 与 futu 版签名对齐（未使用）。
        ctx: 与 futu 版签名对齐（未使用）。
        codes (list[str]): 股票代码列表（未使用）。
        outputdir: 输出目录（未使用；不产生任何文件）。

    Returns:
        None: 按代码现状不会正常返回——console 未导入，调用即抛
        NameError；补上导入后则为打印提示并返回 None。
    """
    console.error(
        'stock_capital_dist is not supported by QMT xtdata. '
        'No equivalent API exists for capital distribution in xtdata.'
    )
