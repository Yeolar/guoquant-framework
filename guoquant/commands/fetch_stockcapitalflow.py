"""
个股资金流抓取命令：从股票清单 CSV 按周期抓取资金流数据并落盘 parquet。

支持的周期见 fetch provider 的 ``p_types`` （i 分时 / d 日 / w 周）；
-k 帮助中列出的 m（月）未实现。每只股票一个文件，写入
``<日期>/fetched_data/flow/<周期>/`` 目录（周期子目录内直接放 parquet）。
依赖：``guoquant.common.fetch.fetch_stock_capital_flow`` （实际抓取）。

资金流数据供 flow 策略与 IC 分析的资金流因子使用。
"""
from datetime import date
from pathlib import Path
import typer
from rich.progress import Progress
from guoquant.common.log import console
from guoquant.common.utils import outp
from guoquant.common.fetch import *


def command(
    file: str = typer.Option(..., '-f', '--file', help='source stock list csv file'),
    outputdir: str = typer.Option(
        '{d:%y%m%d}/fetched_data',
        '-o',
        '--outputdir',
        help='output directory, default={d:%%y%%m%%d}/fetched_data（子目录固定为 flow/<周期>）'),
    k: str = typer.Option('d', '-k', help='i,d,w,m, default=d'),
) -> None:
    """按清单与周期抓取资金流数据（每只股票一个 parquet）。

    读取清单 CSV 首列的股票代码列表，在 ``open_quote_context()`` 共享
    行情上下文内逐只抓取（Progress 显示进度）；输出目录固定为
    ``fetched_data/flow/<周期>/`` （``<周期>`` 即 -k 参数，目录内直接放
    parquet）。

    provider 分支：实际抓取在 ``fetch_stock_capital_flow`` 内完成——
    futu 支持 i/d/w 三档周期（每股间隔至少 1 秒限速）；qmt xtdata 无
    个股资金流接口（仅北向资金聚合口径），对应实现为占位（调用即报错），
    本命令在 qmt provider 下不可用。

    Args:
        file (str): 股票清单 CSV 文件（首列为股票代码，其余列忽略）。
        outputdir (str): 输出根目录模板，默认 ``{d:%y%m%d}/fetched_data``；
            子目录固定为 flow/<周期>。
        k (str): 周期符号 i/d/w/m，默认 d；i 分时、d 日、w 周可用，
            m（月）未实现。
    """
    source = Path(file)
    type = k
    # 输出目录固定为 fetched_data/flow/<周期>/（<周期> 目录内直接放 parquet）
    outputdir = outp(
        outputdir.format(d=date.today()),
        'flow', type,
        is_dir=True)

    codes = []
    with open(source) as fp:
        for line in fp.readlines():
            items = line.split(',')
            codes.append(items[0])

    with open_quote_context() as ctx:
        with Progress(console=console) as progress:
            fetch_stock_capital_flow(
                    progress, ctx, codes, type, outputdir)
