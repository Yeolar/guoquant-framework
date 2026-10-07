"""回测流水线：对指定日期的数据执行回测（单策略或全策略对比）。

依次执行：

1. backtest：以 ``--date`` 为回测截止日（end）与数据/结果目录日期；
   其余参数（root / start / top_n / rebalance / capital / stop_loss /
   output 等）固定用 backtest 命令的默认值。``--strategy all`` 时等价于
   backtest 的 ``--benchmark``：批量对比全部策略。

与 ``pipe_select_stocks`` 等流水线一致：目标日期先经 ``todate()`` 解析，
并按该日期显式构造路径传给步骤命令。
"""
import time
from datetime import date

import typer

from guoquant.common.log import console
from guoquant.common.utils import todate
from guoquant.commands.backtest import command as backtest


def command(
    date: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--date',
        help=f'date, default={date.today():%y%m%d}'),
    strategy: str = typer.Option(
        'stock_strength_strategy',
        '--strategy',
        help='回测策略（打分核心函数全名，见 guoquant.strategies），'
             'default=stock_strength_strategy；传 all 时批量回测全部策略'),
) -> None:
    """执行回测流水线（``--date`` 即回测截止日，``--strategy`` 选策略）。

    目标日期先经 ``todate()`` 解析：回测 ``end`` 用该日期，数据目录
    （``out/<日期>/fetched_data``）与结果目录（``out/<日期>/backtest``）
    也落在该日期下；其余参数固定为 backtest 命令默认值（top_n=10、
    rebalance=5、capital=1000000、stop_loss=0.08、start=数据最早日期）。
    ``strategy`` 传 ``all`` 时等价于 backtest 的 ``--benchmark``
    （批量回测全部注册策略并输出收益对比报告）。

    Args:
        date (str): 目标日期（YYMMDD），作为回测截止日（end）与数据/结果
            目录日期，默认今天。
        strategy (str): 回测策略（打分核心函数全名，见 ``guoquant.strategies``），
            default=``stock_strength_strategy``；传 ``all`` 等价于 benchmark
            模式，批量回测全部策略。
    """
    start = time.time()

    d = todate(date)
    dstr = f'{d:%y%m%d}'
    console.print(f'date={dstr}')

    # strategy=all 等价于 benchmark 批量模式
    benchmark = strategy == 'all'

    console.print('\n[[ BACKTEST ]]')
    # 直接调用 backtest 命令函数：所有带默认值的 typer 参数必须显式传值
    # （漏传会拿到 OptionInfo），此处 end/root/output 按 dstr 解析，
    # 其余参数保持 backtest 命令的默认值。benchmark 模式下 strategy 参数
    # 被 backtest 忽略（遍历注册表全部策略），传默认名即可。
    backtest(
        strategy='stock_strength_strategy' if benchmark else strategy,
        root=f'{dstr}/fetched_data',
        start=None,
        end=dstr,
        top_n=10,
        rebalance=5,
        capital=1_000_000,
        stop_loss=0.08,
        benchmark=benchmark,
        include_flow=False,
        output=f'{dstr}/backtest',
        date=dstr,
    )

    console.print(f'cost={int(time.time() - start)}s', style='green')
