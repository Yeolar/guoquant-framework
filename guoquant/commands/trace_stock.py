"""股票上榜追踪命令

回看最近若干交易日的 rank 排名 CSV，统计每只股票各日各策略的得分，输出
``trace.json``，用于复盘"哪些股票持续出现在榜单/何时开始上榜"。

输出文件：``out/<日期>/rank/trace.json``，结构为 ``{ 代码: [ {date, score,
strategy}, ... ] }``。

依赖：``guoquant.common.name_map.rank_files`` （待追踪的排名文件名列表）；常量
取自 ``guoquant.common.quote`` （``LATEST_N`` 回溯上限）与
``guoquant.common.constants`` （``STOCK_TOP_LIMIT``，``--top`` 默认值）。

注意：CSV 列格式约定——股票策略 4 列（``code, name, plate, score``），主题
策略 3 列（``code, name, score``），score 列位置据此区分。
"""
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from rich.progress import track
import typer
from guoquant.common.utils import outp, todate
from guoquant.common.data_path import *
from guoquant.common.quote import *
from guoquant.common.name_map import rank_files
from guoquant.common.constants import STOCK_TOP_LIMIT, THEME_TOP_LIMIT
from guoquant.strategies import *


def command(
    top: int = typer.Option(
        STOCK_TOP_LIMIT,
        '--top',
        help=f'top count, default={STOCK_TOP_LIMIT} (0=all)'),
    date: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--date',
        help=f'date, default={date.today():%y%m%d}'),
) -> None:
    """回溯统计股票在最近各交易日榜单上的上榜记录。

    从 ``--date`` 往前逐日回溯，找最近 ``LATEST_N`` 个存在 ``rank`` 目录的交易
    日（早于硬编码历史起点 260301 即停止），对每个交易日汇总 ``rank_files``
    各策略榜单的上榜记录。CSV 按列数区分策略类型：4 列 = 股票策略（score 取
    第 4 列），3 列 = 主题策略（score 取第 3 列）。结果写入
    ``out/<日期>/rank/trace.json``。

    Args:
        top (int): 每个榜单只取前 top 名，default=``STOCK_TOP_LIMIT``
            （0 表示全部）。
        date (str): 回溯基准日期（YYMMDD），default=今天。
    """
    d = todate(date)
    today = d
    top = top

    codes = defaultdict(list)

    count = 0
    # 从 --date 往前逐日回溯，最多收集 LATEST_N 个"存在 rank 目录"的交易日
    while count < LATEST_N:
        d -= timedelta(1)
        dstr = f'{d:%y%m%d}'
        if dstr < '260301':
            # 硬编码的历史数据起点边界（早于该日期无 rank 数据）
            break
        root = outp('.') / dstr / 'rank'
        if root.exists():
            for file in rank_files:
                source = root / file
                if source.exists():
                    with open(source) as fp:
                        lines = fp.readlines()
                        if top > 0:
                            lines = lines[:top]  # 只取榜单前 top 名
                        for line in lines:
                            parts = line.split(',')
                            # 列数区分：4 列=股票策略（score 在 parts[3]），
                            # 3 列=主题策略（score 在 parts[2]）
                            codes[parts[0]].append({
                                'date': dstr,
                                'score': float(parts[3]) if len(parts) == 4 else float(parts[2]),
                                'strategy': source.stem,
                            })
            count += 1

    output = outp(f'{today:%y%m%d}/rank/trace.json')
    with open(output, 'w') as fp:
        json.dump(codes, fp, indent=2)
