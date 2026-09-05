"""
资金流抓取（QMT）——不支持的占位实现。

职责：QMT xtdata 不提供个股资金流（分单资金流向）数据，仅提供北向资金
聚合口径（northfinancechange1m / northfinancechange1d）。本模块保留与
futu provider 版（futuapi/stockcapitalflow.py）一致的函数签名
（progress/ctx/codes/cftype/outputdir），使 provider 切换时 fetch 命令
无需分支。本实现不抓取任何数据、不落盘，调用即报错（详见函数 docstring）。

模块级 p_types 仅为占位：键与 futu 版对齐（'i'/'d'/'w'），值是展示用
周期名，函数内未使用。
"""

# 周期符号 → 周期名：键与 futu 版 p_types 对齐（i/d/w），占位实现中未使用
p_types = {
    'i': 'intraday',
    'd': 'day',
    'w': 'week',
}


def fetch_stock_capital_flow(progress, ctx, codes, cftype, outputdir):
    """占位实现：与 futu 版同名同签名，QMT xtdata 不支持个股资金流。

    已知缺陷：函数体引用 console 打印错误提示，但本模块未导入 console
    （缺少 from guoquant.common.log import console），实际调用会先抛
    NameError: name 'console' is not defined，而不是打印出这里的提示；
    即使补上导入，也只是打印提示后返回，不抓取、不落盘。保留同名同
    签名仅为让 fetch 命令按 provider 统一调用（如 fetch_stockdata /
    fetch_stockcapitalflow 命令的 flow 分支）。

    Args:
        progress: 与 futu 版签名对齐（未使用）。
        ctx: 与 futu 版签名对齐（未使用）。
        codes (list[str]): 股票代码列表（未使用）。
        cftype (str): 周期参数名与 futu 版对齐（未使用；模块级 p_types
            亦未被引用）。
        outputdir: 输出目录（未使用；不产生任何文件）。

    Returns:
        None: 按代码现状不会正常返回——console 未导入，调用即抛
        NameError；补上导入后则为打印提示并返回 None。
    """
    console.error(
        'stock_capital_flow is not supported by QMT xtdata. '
        'QMT xtdata only provides northbound aggregate capital flow '
        '(northfinancechange1m / northfinancechange1d).'
    )
