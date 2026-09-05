"""
资金流抓取（富途）——逐股拉取资金流数据并落盘 parquet。

职责：作为 futu provider 的一部分，由 commands/fetch_stockdata.py 与
commands/fetch_stockcapitalflow.py 调用；每只股票一个 parquet 文件，
落盘在 fetched_data/flow/{周期}/ 下（文件名 {code}.parquet），是 flow
类策略（如 stock_flow_strategy）回测时的资金流输入。

周期约定：模块级 p_types 把周期符号映射到富途资金流统计周期——
'i' 日内 / 'd' 日 / 'w' 周。逐股限速依赖
guoquant.common.utils.keep_one_sec（每股间隔至少 1 秒）。
"""
import datetime
import time
from futu import *
from guoquant.common.log import console
from guoquant.common.utils import keep_one_sec


# 周期符号 → 资金流统计周期：'i' 日内 / 'd' 日 / 'w' 周
p_types = {
    'i': PeriodType.INTRADAY,
    'd': PeriodType.DAY,
    'w': PeriodType.WEEK,
}


def fetch_stock_capital_flow(progress, ctx, codes, type, outputdir):
    """批量抓取一批股票的资金流（每只股票一个 parquet 文件）。

    抓取口径：逐只调用 _dump（内部走 get_capital_flow，周期取
    p_types[type]）；若 outputdir 下已存在 {code}.parquet 则直接跳过
    （断点续传）；每次抓取前 keep_one_sec()，保证每股间隔至少 1 秒。

    Args:
        progress: 进度条对象（MultiThreadProgress 或 rich Progress，
            均提供 add_task / update 方法）。
        ctx (futu.OpenQuoteContext): open_quote_context() 返回的行情
            上下文。
        codes (list[str]): 股票代码列表（Futu 格式，如 SH.600000）。
        type (str): 周期符号，取值见模块级 p_types（'i'/'d'/'w'）。
        outputdir (pathlib.Path): 输出目录；每只股票写 {code}.parquet。

    Returns:
        None: 无返回值；结果直接落盘到 outputdir 下的 parquet 文件。
    """
    task = progress.add_task("Working...", total=len(codes))

    for code in codes:
        if (outputdir / f'{code}.parquet').exists():
            progress.update(task,
                            advance=1,
                            description='Skip exist')
            continue

        with keep_one_sec():
            _dump(ctx, outputdir / f'{code}.parquet', code, type)
            progress.update(task,
                            advance=1,
                            description=f'{type}f {code}')


def _dump(ctx, path, code, type):
    """拉取单只股票的资金流并写 parquet。

    抓取口径：调用 get_capital_flow(code, period_type=p_types[type])；
    一次失败后 sleep 1s 重试一次，仍失败则 console.error 打印错误并
    返回（不写文件）。字段处理：丢弃 last_valid_time 列（当前实现未先
    判断该列是否存在，缺失时 drop 会抛 KeyError，属已知现状）；
    capital_flow_item_time 改名为 t 并解析为 datetime 对象，其余资金流
    字段（主力/散户净流入等）保持原样。

    Args:
        ctx (futu.OpenQuoteContext): 行情上下文。
        path (pathlib.Path): 输出 parquet 路径（{code}.parquet）。
        code (str): 股票代码（Futu 格式）。
        type (str): 周期符号，见模块级 p_types。

    Returns:
        None: 无返回值；成功时结果写入 path。
    """
    if path.exists():
        return

    ret, data = ctx.get_capital_flow(code, period_type=p_types[type])
    if ret != RET_OK:
        time.sleep(1)
        ret, data = ctx.get_capital_flow(code, period_type=p_types[type])
    if ret != RET_OK:
        console.error(data)
        return

    df = data.drop('last_valid_time', axis=1).rename(columns={
        'capital_flow_item_time': 't',
        })
    df['t'] = df['t'].apply(lambda x: datetime.strptime(x, '%Y-%m-%d %H:%M:%S'))

    df.to_parquet(path, index=False)
