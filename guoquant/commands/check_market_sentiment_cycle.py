"""市场情绪周期检测命令

用全部个股日线行情计算市场情绪强度 E（近 240 日历史百分位）与动量 M，结果
写入 ``market_sentiment_cycle.txt`` ，供下游 ``guoquant.common.phase`` 读取并
映射情绪阶段（冰点/修复/发酵/高潮/退潮）；rank 板块分支据此对策略做适用阶段
校验（不适用仅提示，仍运行）。

输出文件内容：

- 首段：近 60 日的 E 序列（情绪强度百分位，情绪策略返回项 0）；
- 末行：``E=<最新强度百分位>, M=<情绪动量>``。
下游 ``guoquant.common.phase.read_market_phase`` 只读取末行得到 `` (E, M, 阶段)``。

依赖：``guoquant.strategies.market_sentiment_cycle_strategy`` （情绪指标算法）、
``guoquant.common.quote.read_quote`` （行情读取）。
"""
import json
from datetime import date
from pathlib import Path
from rich.progress import Progress, track
import typer
from guoquant.common.log import console, info
from guoquant.common.parallel import parallel
from guoquant.common.utils import outp, todate
from guoquant.common.data_path import *
from guoquant.common.quote import *
from guoquant.strategies import *


def command(
    root: str = typer.Option(
        '{d:%y%m%d}/fetched_data',
        '--root',
        help='root directory, default={d:%%y%%m%%d}/fetched_data'),
    output: str = typer.Option(
        '{d:%y%m%d}/rank/market_sentiment_cycle.txt',
        '-o',
        '--output',
        help='output txt file, default={d:%%y%%m%%d}/rank/market_sentiment_cycle.txt'),
    date: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--date',
        help=f'date, default={date.today():%y%m%d}'),
) -> None:
    """检测市场情绪周期，输出 E（情绪强度百分位）与 M（情绪动量）到 txt 文件。

    使用截至 ``--date`` 的全市场个股日线（需达情绪策略 ``window`` 的历史长度）
    计算：文件首段落近 60 日的 E 序列，末行落 ``E=…, M=…`` （下游
    ``read_market_phase`` 只解析末行）。默认输出路径即 rank 板块分支读取的情绪
    文件（rank 中按 ``output.with_name('market_sentiment_cycle.txt')`` 定位），
    故运行板块类 rank 前应先执行本命令。

    Args:
        root (str): 数据根目录，default=``{d:%y%m%d}/fetched_data``。
        output (str): 输出 txt 文件路径，default=
            ``{d:%y%m%d}/rank/market_sentiment_cycle.txt``。
        date (str): 用于 ``root`` / ``output`` 模板的日期（YYMMDD），
            default=今天。
    """
    d = todate(date)
    root = outp(root.format(d=d))
    output = outp(output.format(d=d))

    console.print(f'check market sentiment cycle -> {output}')

    paths = d_data_paths(root)

    def _read(path):
        # 行情截断到 d：只用截至当前交易日的全市场个股数据
        return read_quote(path, end_date=d)

    quotes = []
    ignored = 0
    for q in parallel(_read, paths, description='Loading...'):
        # 需要较长历史（window=560 个交易日）才能计算情绪强度百分位，不足的剔除
        if len(q) >= market_sentiment_cycle_strategy.window:
            quotes.append(q)
        else:
            ignored += 1
    console.print(f'ignore n<{market_sentiment_cycle_strategy.window}'
                  f' stock count={ignored}', style='yellow bold')

    # 情绪策略返回 [近60日 E 序列, 最新 E, 动量 M]（见 market_sentiment_cycle_strategy）；
    # 首行把整段 E 序列落盘，末行落 E=…, M=…（下游 phase 只解析末行）
    result = market_sentiment_cycle_strategy(
            track(quotes, console=console))

    with open(output, 'w') as fp:
        e_pct, e, m = result
        item = f'E={e}, M={m}'
        fp.write(f'{e_pct}\n')
        fp.write(item + '\n')

        # 多行表格文本用 info(..., pretty=True) 去缩进（rich Console.print
        # 无 pretty 参数，见 guoquant.common.log.info）
        info("""
        | E         | M         | 状态      |
        |-----------|-----------|-----------|
        | <0.1      | <=0       | 冰点      |
        | <0.3      |  >0       | 修复      |
        |  0.3~0.7  |  >0       | 发酵      |
        | >0.7      |  >0       | 高潮      |
        | >0.7      |  <0       | 退潮      |
        """, pretty=True)
        console.print(item)
