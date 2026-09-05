"""
个股筹码分布抓取命令：从股票清单 CSV 逐只抓取筹码分布快照并落盘 parquet。

筹码分布为当日快照、无日/周等周期维度；每只股票一个文件，
写入 ``<日期>/fetched_data/dist/{code}.parquet`` （dist 目录内直接平铺）。
依赖：``guoquant.common.fetch.fetch_stock_capital_dist`` （实际抓取）。

筹码快照与 flow 数据配套，供 flow 引擎（``stock_flow_strategy`` 等
needs_flow 策略）使用：组装 Quote 时经 ``read_quote`` / ``load_dist_record``
按 dist 路径自动加载（取文件末行的最新快照）。

注意：快照仅覆盖最新一日、无跨日历史，历史回测中直接使用存在前视偏差
（见 ``guoquant.common.quote`` 说明）。
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
        help='output directory, default={d:%%y%%m%%d}/fetched_data（子目录固定为 dist）'),
) -> None:
    """按清单逐只抓取筹码分布快照（每只股票一个 parquet）。

    读取清单 CSV 首列的股票代码列表，在 ``open_quote_context()`` 共享
    行情上下文内逐只抓取（Progress 显示进度）；输出目录固定为
    ``fetched_data/dist/`` （筹码快照不分周期，目录内直接放 parquet）。

    provider 分支：实际抓取在 ``fetch_stock_capital_dist`` 内完成——
    futu 逐股拉取当日筹码快照（每股间隔至少 1 秒限速）；qmt xtdata 无
    筹码分布接口，对应实现为占位（调用即报错），本命令在 qmt provider
    下不可用。

    Args:
        file (str): 股票清单 CSV 文件（首列为股票代码，其余列忽略）。
        outputdir (str): 输出根目录模板，默认 ``{d:%y%m%d}/fetched_data``；
            子目录固定为 dist。
    """
    source = Path(file)
    # 输出目录固定为 fetched_data/dist/（筹码快照不分周期，目录内直接放 parquet）
    outputdir = outp(
        outputdir.format(d=date.today()),
        'dist',
        is_dir=True)

    codes = []
    with open(source) as fp:
        for line in fp.readlines():
            items = line.split(',')
            codes.append(items[0])

    with open_quote_context() as ctx:
        with Progress(console=console) as progress:
            fetch_stock_capital_dist(
                    progress, ctx, codes, outputdir)
