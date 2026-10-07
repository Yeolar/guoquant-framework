"""选股流水线：数据同步 → 市场情绪 → 主题排名 → 全策略排名 → 追踪 → 报告 → 归档。

依次执行：

1. 远端数据同步：数据目录缺失时 rsync（见 ``guoquant.common.sync``）；
2. check_market_sentiment_cycle：市场情绪周期判断；
3. rank theme_strength：主题强度排名（industry / concept 各一次）；
4. 股票策略排名：STOCK_STRATEGIES 中 6 个策略 × industry/concept，
   另加 stock_rise_rate_strategy（无板块维度，单独一次）；
5. trace_stock：追踪记录；
6. report：rank_files 逐个生成报告 JSON；
7. copy_data：数据归档。

移植自 guozi-quant pipeline 的同名命令。与原版差异：

- ``rank_*`` 系列已合并为单一 rank 命令（--strategy 动态解析），此处按策略名调用；
- 远端同步配置从 .env 读取（MACMINI_IP / MACMINI_USERNAME /
  MACMINI_PASSWORD、MACMINI_OUT_DIR），未配置时跳过。
"""
import os
import time
from datetime import date, timedelta

import typer

from guoquant.commands.check_market_sentiment_cycle import (
    command as check_market_sentiment_cycle,
)
from guoquant.commands.copy_data import command as copy_data
from guoquant.commands.rank import command as rank
from guoquant.commands.report import command as report
from guoquant.commands.trace_stock import command as trace_stock
from guoquant.common.log import console
from guoquant.common.name_map import rank_files
from guoquant.common.sync import fetch_remote
from guoquant.common.utils import outp, todate
from guoquant.common.constants import STOCK_TOP_LIMIT

# (策略名, 中文说明)：策略名即打分核心函数全名；无板块策略单独处理
STOCK_STRATEGIES = [
    ('stock_ma_cluster_breakout_strategy', '均线收敛突破策略'),
    ('stock_lowend_startup_strategy', '低位启动策略'),
    ('stock_pullback_strategy', '回调策略'),
    ('stock_strength_strategy', '龙头强化策略'),
    ('stock_flow_strategy', '资金流策略'),
    ('stock_main_wave_strategy', '主升浪策略'),
]


def command(
    date: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--date',
        help=f'date, default={date.today():%y%m%d}'),
    offset: int = typer.Option(
        0,
        '--offset',
        help='date offset, default=0'),
    skip_strategy: bool = typer.Option(
        False,
        '--skip-strategy',
        help='skip strategies'),
) -> None:
    """执行数据同步 → 全策略排名 → 追踪 → 报告 → 归档的选股流水线。

    目标日期先经 ``todate()`` 解析、再回退 offset 天。步骤细节：

    - 数据目录缺失时先经 ``fetch_remote`` 从远端同步（``MACMINI_*`` .env 配置）；
    - skip_strategy 为假时依次跑市场情绪、theme_strength（industry /
      concept）、STOCK_STRATEGIES 中 6 个股票策略（× industry/concept）
      与涨幅榜策略的 rank，再执行 trace_stock；
    - report 按 rank_files 逐个生成报告 JSON（涨幅榜文件不限 top 数，
      其余文件取 ``STOCK_TOP_LIMIT``），最后 copy_data 归档。

    Args:
        date (str): 目标日期（YYMMDD），默认今天。
        offset (int): 相对 date 的回退天数，默认 0。
        skip_strategy (bool): 为真时跳过策略排名与追踪步骤（仅对已存在
            的 rank 结果做报告与归档）。
    """
    start = time.time()

    d = todate(date) - timedelta(offset)
    dstr = f'{d:%y%m%d}'
    console.print(f'date={dstr}')

    root = outp(f'{dstr}/')
    if not root.exists():
        console.print('\n[[ FETCH STOCK DATA ]]')
        remote_out = os.environ.get(
            'MACMINI_OUT_DIR', '/Users/yeolar/dev/guozi/guozi-command/out')
        fetch_remote(root, f'{remote_out}/{dstr}/')

    if not skip_strategy:
        # 各步骤命令的参数默认值是 typer.Option(...) 对象：直接调用命令函数时
        # 漏传任何带默认值的参数都会拿到 OptionInfo（AttributeError），且其
        # 默认路径模板用 date.today() 填充、与 --date 的 dstr 不一致。因此
        # 所有带默认值的参数都按 dstr 显式传值。
        fetched = f'{dstr}/fetched_data'

        console.print('\n[[ CHECK MARKET SENTIMENT ]]')
        check_market_sentiment_cycle(
            root=fetched,
            output=f'{dstr}/rank/market_sentiment_cycle.txt',
            date=dstr)

        console.print('\n[[ RANK THEME STRENGTH ]]')
        rank(strategy='theme_strength', root=fetched, output=None,
            type='industry', date=dstr)
        rank(strategy='theme_strength', root=fetched, output=None,
            type='concept', date=dstr)

        for strategy, label in STOCK_STRATEGIES:
            console.print(f'\n[[ {label}(RANK STOCK {strategy.upper()}) ]]')
            rank(strategy=strategy, root=fetched, output=None,
                type='industry', date=dstr)
            rank(strategy=strategy, root=fetched, output=None,
                type='concept', date=dstr)

        console.print('\n[[ 涨幅榜策略(RANK STOCK RISE RATE) ]]')
        rank(strategy='stock_rise_rate_strategy', root=fetched, output=None,
            date=dstr)

        console.print('\n[[ TRACE ]]')
        trace_stock(top=STOCK_TOP_LIMIT, date=dstr)
    else:
        console.print('skip strategies', style='yellow bold')

    console.print('\n[[ REPORT ]]')
    for filename in rank_files:
        file = outp(f'{dstr}/rank/{filename}')
        if file.exists():
            top = STOCK_TOP_LIMIT if filename != 'stock_rise_rate_strategy_score.csv' else 0
            report(file=file, top=top, root=f'{dstr}/render', date=dstr)

    console.print('\n[[ COPY DATA ]]')
    copy_data(date=dstr)

    console.print(f'cost={int(time.time() - start)}s', style='green')
