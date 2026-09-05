"""
个股行情抓取命令：从股票清单 CSV 抓取单个周期的 K 线行情并落盘 parquet。

周期 k = 1/3/5 分钟、d 日线、w 周线；每只股票一个文件，写入
``<日期>/fetched_data/quote/<周期>/stock/`` （stock/ 与 index/ 平行）。
依赖：``guoquant.common.fetch.fetch_stock_quote`` （实际抓取，parquet 落地）。
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
        help='output directory, default={d:%%y%%m%%d}/fetched_data（子目录固定为 quote/<周期>/stock）'),
    k: str = typer.Option('d', '-k', help='1,3,5,d,w, default=d'),
) -> None:
    """按指定周期抓取清单中所有股票的 K 线行情。

    读取清单 CSV 首列的股票代码列表，在 ``open_quote_context()`` 共享
    行情上下文内批量抓取（Progress 显示进度）；输出目录固定为
    ``fetched_data/quote/<周期>/stock/`` （stock/ 与 index/ 平行）。

    provider 分支：实际抓取在 ``fetch_stock_quote`` 内完成，按 .env 的
    ``FETCH_PROVIDER`` 分发——futu 分批订阅（每批 300 只上限）后逐股
    拉取最近 1000 根前复权 K 线；qmt 从 xtdata 下载历史数据后转置为逐行
    K 线。两者产物列统一为短名（t/o/c/h/l/v/a/tr/lc），t 为 datetime、
    tr 为百分比（%）。

    行为：已存在的 parquet 直接跳过（断点续传）；单只抓取失败仅打印
    错误并继续，订阅/退订失败会中止剩余抓取。

    Args:
        file (str): 股票清单 CSV 文件（首列为股票代码，其余列忽略）。
        outputdir (str): 输出根目录模板，默认 ``{d:%y%m%d}/fetched_data``；
            子目录固定为 quote/<周期>/stock。
        k (str): 周期符号 1/3/5/d/w（分钟线/日线/周线），默认 d。
    """
    source = Path(file)
    type = k
    # 输出目录固定为 fetched_data/quote/<周期>/stock/（stock/ 与 index/ 平行）
    outputdir = outp(
        outputdir.format(d=date.today()),
        'quote', type, 'stock',
        is_dir=True)

    codes = []
    with open(source) as fp:
        for line in fp.readlines():
            items = line.split(',')
            codes.append(items[0])

    with open_quote_context() as ctx:
        with Progress(console=console) as progress:
            # 单只抓取失败仅打印错误并继续；订阅/退订失败会中止剩余抓取
            fetch_stock_quote(
                    progress, ctx, codes, type, outputdir)
