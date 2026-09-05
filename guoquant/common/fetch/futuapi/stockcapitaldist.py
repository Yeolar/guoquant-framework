"""
筹码分布抓取（富途）——逐股拉取筹码分布数据并落盘 parquet。

职责：作为 futu provider 的一部分，由 commands/fetch_stockdata.py 与
commands/fetch_stockcapitaldist.py 调用；每只股票一个 parquet 文件，
落盘在 fetched_data/dist/ 下（文件名 {code}.parquet），是 flow 类策略
（如 stock_flow_strategy）回测时的筹码分布输入。

约定：筹码分布为当日快照，无日/周等周期维度；逐股限速依赖
guoquant.common.utils.keep_one_sec（每股间隔至少 1 秒）。
"""
import datetime
import time
from futu import *
from guoquant.common.log import console
from guoquant.common.utils import keep_one_sec


def fetch_stock_capital_dist(progress, ctx, codes, outputdir):
    """批量抓取一批股票的筹码分布（每只股票一个 parquet 文件）。

    抓取口径：逐只调用 _dump（内部走 get_capital_distribution）；若
    outputdir 下已存在 {code}.parquet 则直接跳过（断点续传）；每次抓取
    前 keep_one_sec()，保证每股间隔至少 1 秒。

    Args:
        progress: 进度条对象（MultiThreadProgress 或 rich Progress，
            均提供 add_task / update 方法）。
        ctx (futu.OpenQuoteContext): open_quote_context() 返回的行情
            上下文。
        codes (list[str]): 股票代码列表（Futu 格式，如 SH.600000）。
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
            _dump(ctx, outputdir / f'{code}.parquet', code)
            progress.update(task,
                            advance=1,
                            description=f'.d {code}')


def _dump(ctx, path, code):
    """拉取单只股票的筹码分布并写 parquet。

    抓取口径：调用 get_capital_distribution(code)；一次失败后 sleep 1s
    重试一次（接口偶发失败较多），仍失败则 console.error 打印错误并
    返回（不写文件）。字段转换：update_time 改名为 t 并解析为 datetime
    对象；解析失败仅打印告警，t 保留原字符串继续写入——因此不同文件的
    t 列类型可能不一致（已知现状，未做统一处理）。

    Args:
        ctx (futu.OpenQuoteContext): 行情上下文。
        path (pathlib.Path): 输出 parquet 路径（{code}.parquet）。
        code (str): 股票代码（Futu 格式）。

    Returns:
        None: 无返回值；成功时结果写入 path。
    """
    if path.exists():
        return

    ret, data = ctx.get_capital_distribution(code)
    if ret != RET_OK:
        time.sleep(1)
        ret, data = ctx.get_capital_distribution(code)
    if ret != RET_OK:
        console.error(data)
        return

    df = data.rename(columns={
        'update_time': 't',
        })
    try:
        df['t'] = df['t'].apply(lambda x: datetime.strptime(x, '%Y-%m-%d %H:%M:%S'))
    except Exception as e:
        console.error(f'{e}, {code}: {df["t"]}')

    df.to_parquet(path, index=False)
