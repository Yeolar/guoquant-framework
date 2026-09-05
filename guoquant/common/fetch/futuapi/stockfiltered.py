"""
股票筛选抓取（富途）——按流通市值过滤全市场股票并落盘 CSV。

职责：作为 futu provider 的一部分，由 commands/fetch_stockfiltered.py
调用（按市值分层相邻区间逐档调用本函数）；产物 stock_mv_{档}.csv
（无表头，每行 "code, name, 流通市值(元)"）被
guoquant.common.trade.data.load_filtered_stocks 与
guoquant.common.data_path.filtered_stock_paths 读取，作为 auto_trade、
rank 及 ``pipe_*`` 系列命令的市值分层候选池。

口径约定：市值过滤（SimpleFilter + StockField.FLOAT_MARKET_VAL）在函数
内构造，调用方只需传市值区间（亿元）；富途市值字段单位为元，代码内按
亿元 ×1e8 换算。
"""
import time
from guoquant.common.log import console
from futu import *


def fetch_stock_filtered(progress, ctx, output, min_mv, max_mv=None):
    """按流通市值区间分页拉取全市场筛选结果，过滤 ST 股后写 CSV。

    筛选口径：函数内构造 SimpleFilter（筛选字段
    StockField.FLOAT_MARKET_VAL、按流通市值降序），对 Market.SH 调
    get_stock_filter 分页取全市场；min_mv / max_mv 单位为亿元，构造时
    ×1e8 换算成元。输出 CSV 无表头，每行 "code, name, value"：value 为
    流通市值（元，与 FLOAT_MARKET_VAL 口径一致，rank 等下游按元使用）。

    过程约定：

    - 以 begin=len(results) 翻页，直到服务端返回 last_page=True；
    - 每页（含最后一页）间隔 3s 限速，避免触发富途风控；
    - 名称含 'ST' 的股票被排除（基本面风险股）。

    已知现状：单页失败仅 console.error 打印错误后照常 sleep 3s 并继续
    循环，翻页游标不推进——若服务端持续失败会陷入无限循环，且函数不会
    因此终止或产出文件。

    Args:
        progress: 进度条对象（MultiThreadProgress 或 rich Progress）。
        ctx (futu.OpenQuoteContext): open_quote_context() 返回的行情
            上下文。
        output (str): 输出 CSV 文件路径。
        min_mv (float): 市值下限（亿元）。
        max_mv (float, optional): 市值上限（亿元）；为 None 或 0 时按
            truthy 判断视为不限上限。

    Returns:
        None: 无返回值；成功时结果直接写入 output 文件。
    """
    # 市值过滤在函数内构造：富途市值字段单位为元，min_mv/max_mv（亿）×1e8
    f = SimpleFilter()
    f.filter_min = 100000000 * min_mv
    if max_mv:
        f.filter_max = 100000000 * max_mv
    f.stock_field = StockField.FLOAT_MARKET_VAL
    f.is_no_filter = False
    f.sort = SortDir.DESCEND

    task = progress.add_task("Working...", total=1000)

    results = []
    last_page = False
    while not last_page:
        ret, data = ctx.get_stock_filter(
                market=Market.SH,
                filter_list=[f],
                begin=len(results))
        if ret == RET_OK:
            last_page, all_count, batch = data
            results.extend(batch)
            progress.update(task,
                            total=all_count,
                            completed=len(results))
        else:
            console.error(data)
        time.sleep(3)

    with open(output, 'w') as fp:
        for item in results:
            if 'ST' not in item.stock_name:
                # item[f] 等价于 item.float_market_val（按筛选字段取属性值）
                result = f'{item.stock_code}, {item.stock_name}, {item[f]}'
                fp.write(result + '\n')
