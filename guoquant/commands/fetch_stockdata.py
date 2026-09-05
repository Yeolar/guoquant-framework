"""
批量抓取股票数据命令：从股票清单 CSV 并发抓取行情、资金流与筹码分布三类数据。

三类抓取各自跑一个线程并发执行，共享同一行情连接上下文 ctx：

1. 行情 quote：日线 + 周线，写 ``<日期>/fetched_data/quote/d|w/stock/{code}.parquet``；
2. 资金流 flow：日线，写 ``<日期>/fetched_data/flow/d/{code}.parquet``；
3. 筹码分布 dist：当日快照、不分周期，写 ``<日期>/fetched_data/dist/{code}.parquet``。

依赖：``guoquant.common.fetch.*`` （fetch_stock_quote /
fetch_stock_capital_flow / fetch_stock_capital_dist 与 open_quote_context）。
清单格式：CSV 首列为股票代码（Futu 格式，如 ``SZ.000001``），其余列忽略。
"""
import threading
from datetime import date
from pathlib import Path
import typer
from guoquant.common.log import console
from guoquant.common.parallel import MultiThreadProgress
from guoquant.common.utils import outp
from guoquant.common.fetch import *


def command(
    file: str = typer.Option(..., '-f', '--file', help='source stock list csv file'),
    outputdir: str = typer.Option(
        '{d:%y%m%d}/fetched_data',
        '-o',
        '--outputdir',
        help='output directory, default={d:%%y%%m%%d}/fetched_data'
             '（子目录固定：quote/<周期>/stock、flow/<周期>、dist）'),
    k: str = typer.Option('d', '-k', help='1,3,5,d,w, default=d'),
) -> None:
    """并发抓取行情/资金流/筹码分布三类数据（三个线程并行）。

    在 ``open_quote_context()`` 共享行情上下文内启动三个线程并行抓取
    （``MultiThreadProgress`` 统一显示进度，线程互不依赖）：

    - 行情：日线 + 周线两段，分别写 ``quote/d/stock/`` 与 ``quote/w/stock/``；
    - 资金流：日线，写 ``flow/d/`` （目录内直接放 parquet）；
    - 筹码分布：当日快照，写 ``dist/`` （不分周期，目录内直接放 parquet）。

    各抓取函数内部做断点续传（已有 parquet 跳过）并限速。

    provider 分支：实际抓取函数按 .env 的 ``FETCH_PROVIDER`` 分发——
    futu 三类均可抓；qmt 下筹码分布/资金流为占位实现（调用即报错），
    仅行情可抓。

    Args:
        file (str): 股票清单 CSV 文件（首列为股票代码，其余列忽略）。
        outputdir (str): 输出根目录模板，默认 ``{d:%y%m%d}/fetched_data``；
            子目录固定：quote/<周期>/stock、flow/<周期>、dist。
        k (str): 周期参数，默认 d；当前实现未使用——三类抓取的周期在
            函数内固定（行情 d/w、资金流 d、筹码分布不分周期）。
    """
    source = Path(file)
    type = k
    base = outp(outputdir.format(d=date.today()), is_dir=True)

    codes = []
    with open(source) as fp:
        for line in fp.readlines():
            items = line.split(',')
            codes.append(items[0])

    def _fetch_stock_quote(progress, ctx, codes):
        """并发任务一：抓取日线与周线两段个股行情。

        分别写 ``quote/d/stock/`` 与 ``quote/w/stock/`` （stock/ 与 index/
        平行，每只股票一个 parquet）。

        Args:
            progress: MultiThreadProgress 实例，供抓取函数展示进度。
            ctx: ``open_quote_context()`` 返回的行情上下文。
            codes: 股票代码列表（Futu 格式）。
        """
        # 日线行情：quote/d/stock/（stock/ 与 index/ 平行）
        outdir = outp(base, 'quote', 'd', 'stock', is_dir=True)
        fetch_stock_quote(progress, ctx, codes, 'd', outdir)
        # 周线行情：quote/w/stock/
        outdir = outp(base, 'quote', 'w', 'stock', is_dir=True)
        fetch_stock_quote(progress, ctx, codes, 'w', outdir)

    def _fetch_stock_capital_flow(progress, ctx, codes):
        """并发任务二：抓取日线资金流。

        写 ``flow/d/`` （目录内直接放 parquet）。

        Args:
            progress: MultiThreadProgress 实例，供抓取函数展示进度。
            ctx: ``open_quote_context()`` 返回的行情上下文。
            codes: 股票代码列表（Futu 格式）。
        """
        # 资金流：flow/d/（目录内直接放 parquet）
        outdir = outp(base, 'flow', 'd', is_dir=True)
        fetch_stock_capital_flow(progress, ctx, codes, 'd', outdir)

    def _fetch_stock_capital_dist(progress, ctx, codes):
        """并发任务三：抓取当日筹码分布快照。

        写 ``dist/`` （不分周期，目录内直接放 parquet）。

        Args:
            progress: MultiThreadProgress 实例，供抓取函数展示进度。
            ctx: ``open_quote_context()`` 返回的行情上下文。
            codes: 股票代码列表（Futu 格式）。
        """
        # 筹码分布：dist/（不分周期，目录内直接放 parquet）
        outdir = outp(base, 'dist', is_dir=True)
        fetch_stock_capital_dist(progress, ctx, codes, outdir)

    with open_quote_context() as ctx:
        with MultiThreadProgress(console=console,
                                 transient=True) as progress:
            # 三个抓取任务并行：行情、资金流、筹码分布互不依赖
            ts = [
                threading.Thread(
                    target=_fetch_stock_quote,
                    args=(progress, ctx, codes)),
                threading.Thread(
                    target=_fetch_stock_capital_flow,
                    args=(progress, ctx, codes)),
                threading.Thread(
                    target=_fetch_stock_capital_dist,
                    args=(progress, ctx, codes)),
            ]
            [t.start() for t in ts]
            [t.join() for t in ts]
