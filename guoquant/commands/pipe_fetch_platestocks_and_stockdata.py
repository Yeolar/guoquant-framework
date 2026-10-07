"""数据获取流水线：板块列表 → 板块成分 → 板块指数 → 过滤股票 → 行情/K线。

依次执行：

1. fetch_platelist：抓取行业/概念板块列表；
2. fetch_platestocks：逐板块抓取成分股；
3. rewrite_plateindexes：合并板块为 ``plate_index.csv`` （开头含四个基准指数）；
4. fetch_stockfiltered：抓取市值分层股票清单（四档 CSV）；
5. fetch_stockquote：以 ``plate_index.csv`` 为清单先抓日线、再抓周线行情
   （板块/指数 K 线写入 ``quote/<周期>/index/``，与个股的 stock/ 平行）；
6. fetch_stockdata：以四个市值分组清单各抓取一批行情/资金流/筹码数据。

移植自 guozi-quant pipeline 的同名命令（Django call_command 版），
改为直接调用 guoquant 各步骤命令函数。
"""
import time
from datetime import date, timedelta

import typer

from guoquant.common.log import console
from guoquant.common.utils import outp, todate
from guoquant.commands.fetch_platelist import command as fetch_platelist
from guoquant.commands.fetch_platestocks import command as fetch_platestocks
from guoquant.commands.fetch_stockdata import command as fetch_stockdata
from guoquant.commands.fetch_stockfiltered import command as fetch_stockfiltered
from guoquant.commands.fetch_stockquote import command as fetch_stockquote
from guoquant.commands.rewrite_plateindexes import command as rewrite_plateindexes
from guoquant.common.data_path import filtered_stock_paths


def command(
    date: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--date',
        help=f'date, default={date.today():%y%m%d}'),
    offset: int = typer.Option(
        0,
        '--offset',
        help='date offset, default=0'),
) -> None:
    """执行板块/行情与 K 线数据获取流水线（步骤顺序见模块说明）。

    目标日期先经 ``todate()`` 解析、再回退 offset 天；各步骤命令以该
    日期解析出的具体路径（而非各自默认的当天路径）运行。串联关系：
    ``plate_index.csv`` （四个基准指数 + 全部板块）作为 fetch_stockquote
    的清单输入抓 d/w 两段行情（板块/指数 K 线写入 ``quote/<周期>/index/``），
    四个市值分组 CSV（``filtered_stock_paths`` 定位）作为 fetch_stockdata
    的清单输入分四批抓取。

    Args:
        date (str): 目标日期（YYMMDD），默认今天。
        offset (int): 相对 date 的回退天数，默认 0。
    """
    start = time.time()

    d = todate(date) - timedelta(offset)
    dstr = f'{d:%y%m%d}'
    console.print(f'date={dstr}')

    # 各步骤命令的参数默认值是 typer.Option(...) 对象：直接调用命令函数时
    # 不传参会拿到 OptionInfo（AttributeError: 'OptionInfo' object has no
    # attribute 'format'），且步骤内部用 date.today() 填路径，与 --date
    # 指定的 dstr 不一致。因此这里按 dstr 显式构造路径传给各步骤，
    # 与 pipe_select_stocks 的做法保持一致。
    plate_dir = f'{dstr}/fetched_data/plate'

    console.print('\n[[ FETCH PLATE LIST ]]')
    fetch_platelist(outputdir=plate_dir)

    console.print('\n[[ FETCH PLATE STOCKS ]]')
    fetch_platestocks(
        file=plate_dir + '/{type}_plate_list.json',
        output=plate_dir + '/{type}_plates/{code}_{name}.json',
    )

    console.print('\n[[ REWRITE PLATE INDEXES ]]')
    rewrite_plateindexes(
        file=plate_dir + '/{type}_plate_list.json',
        output=f'{dstr}/plate_index.csv',
    )

    console.print('\n[[ FETCH FILTERED STOCKS ]]')
    fetch_stockfiltered(output=f'{dstr}/stockfiltered/' + 'stock_{f}.csv')

    plates = outp(f'{dstr}/plate_index.csv')

    stocks_mv_1, stocks_mv_2, stocks_mv_3, stocks_mv_4 = filtered_stock_paths(
        outp(f'{dstr}/fetched_data'))

    console.print('\n[[ FETCH STOCK QUOTE d ]]')
    fetch_stockquote(k='d', category='index', file=plates,
                     outputdir=f'{dstr}/fetched_data')
    console.print('\n[[ FETCH STOCK QUOTE w ]]')
    fetch_stockquote(k='w', category='index', file=plates,
                     outputdir=f'{dstr}/fetched_data')

    for i, stocks in enumerate([stocks_mv_1, stocks_mv_2, stocks_mv_3, stocks_mv_4], 1):
        console.print(f'\n[[ FETCH STOCK DATA {i}/4 ]]')
        fetch_stockdata(k='d', file=stocks, outputdir=f'{dstr}/fetched_data')

    console.print(f'cost={int(time.time() - start)}s', style='green')
