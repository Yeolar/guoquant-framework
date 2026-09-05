"""
市值分层股票清单抓取命令：按流通市值区间抓取 A 股清单并落盘 CSV。

产物：``<日期>/stockfiltered/stock_mv_{下限}.csv`` （与 fetched_data 同级；
无表头，每行 ``code, name, 流通市值(元)`` ；市值过滤与剔除 ST 均在
``fetch_stock_filtered`` 内完成）。

市值分层来自 ``MV_STEP = [10, 30, 100, 300]`` （亿元）的相邻区间：
10~30 亿、30~100 亿、100~300 亿、≥300 亿，对应四个文件。

下游读取（经 ``common.data_path.filtered_stock_paths`` 与
``common.trade.data.load_filtered_stocks`` 定位）：

- rank：板块/无板块分支的市值输入；
- report：排名股票市值展示；
- auto_trade：实盘候选池限制；
- pipe_fetch_platestocks_and_stockdata：抓取股票数据的清单。

依赖：``guoquant.common.fetch.fetch_stock_filtered`` （实际抓取）、
``guoquant.common.data_path.MV_STEP`` （市值分层定义）。
"""
from datetime import date
import typer
from rich.progress import Progress
from guoquant.common.log import console
from guoquant.common.utils import outp
from guoquant.common.data_path import MV_STEP
from guoquant.common.fetch import *


def command(
    output: str = typer.Option(
        '{d:%y%m%d}/stockfiltered/stock_{f}.csv',
        '-o',
        '--output',
        help='output file, default={d:%%y%%m%%d}/stockfiltered/stock_{f}.csv'),
) -> None:
    """抓取全部市值分层的股票清单（每档一个 CSV）。

    遍历 ``MV_STEP`` 相邻两项构成的 [下限, 上限) 区间，最后一档只有
    下限（≥300 亿）；每档输出一个文件（``stock_mv_{下限}.csv`` ），
    已存在则跳过（避免重复抓取）。

    provider 分支：实际抓取在 ``fetch_stock_filtered`` 内完成，按 .env
    的 ``FETCH_PROVIDER`` 分发——futu 用服务端筛选接口按市值分页拉取；
    qmt 在本地用全市场列表 × 日线收盘价 × 流通股本计算市值后过滤。
    两者产物格式一致：每行 ``code, name, 流通市值(元)``。

    Args:
        output (str): 输出文件路径模板，默认
            ``{d:%y%m%d}/stockfiltered/stock_{f}.csv`` ；``{d}`` 填当天
            日期、``{f}`` 由 fetch() 按市值下限填 ``mv_{lo}``。
    """
    with open_quote_context() as ctx:
        # MV_STEP 相邻两项构成 [下限, 上限] 区间；最后一档只有下限（≥300 亿）
        for i in range(len(MV_STEP)):
            lo = MV_STEP[i]
            hi = MV_STEP[i + 1] if i + 1 < len(MV_STEP) else None
            fetch(ctx, output, lo, hi)


def fetch(ctx, output_opt, lo, hi):
    """抓取单个市值区间的股票清单（目标文件已存在则跳过）。

    实际抓取委托 ``fetch_stock_filtered`` （Progress 显示进度）。

    Args:
        ctx: ``open_quote_context()`` 返回的行情上下文。
        output_opt (str): 输出文件路径模板，含 ``{d}``/``{f}`` 占位；
            ``{f}`` 由本函数填 ``mv_{lo}``。
        lo (int): 市值下限（亿元）。
        hi (``int | None`` ): 市值上限（亿元）；None 表示不限上限（最后一档）。
    """
    output = outp(output_opt.format(d=date.today(), f=f'mv_{lo}'))
    if output.exists():
        # 已存在则跳过，避免重复抓取
        console.print(f'already exist {output}', style='yellow bold')
        return

    console.print(f'fetch filtered stocks -> {output}')

    with Progress(console=console) as progress:
        fetch_stock_filtered(progress, ctx, output, lo, hi)
