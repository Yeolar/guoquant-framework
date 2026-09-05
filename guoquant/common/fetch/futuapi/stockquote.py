"""
K 线行情抓取（富途）——批量拉取日/周/分钟 K 线并落盘 parquet。

职责：作为 futu provider 的一部分，由 commands/fetch_stockdata.py 与
commands/fetch_stockquote.py 调用；每只股票一个 parquet 文件，落盘在
fetched_data/quote/{周期}/stock/ 下（目录约定见 guoquant.common.data_path；
回测与打分只用 d/w），是回测与实盘打分的历史行情来源。

周期约定：模块级 sub_types（订阅）与 kl_types（取 K 线）共同定义
'1'/'3'/'5' 分钟、'd' 日线、'w' 周线的符号映射，两者必须一一对应；
模块级 SUBSCRIBE_LIMIT 为单次订阅数量上限（与行情套餐额度相关）。
逐批限速依赖 guoquant.common.utils.keep_one_minute。
"""
import datetime
from futu import *
from guoquant.common.log import console
from guoquant.common.utils import keep_one_minute


# 周期符号 → 订阅类型（subscribe）与 K 线类型（get_cur_kline）的映射，
# 两者必须一一对应：'d' 日线 / 'w' 周线 / '1'/'3'/'5' 分钟线
sub_types = {
    '1': SubType.K_1M,
    '3': SubType.K_3M,
    '5': SubType.K_5M,
    'd': SubType.K_DAY,
    'w': SubType.K_WEEK,
}

kl_types = {
    '1': KLType.K_1M,
    '3': KLType.K_3M,
    '5': KLType.K_5M,
    'd': KLType.K_DAY,
    'w': KLType.K_WEEK,
}

# 单次订阅数量上限（与行情套餐额度相关：约 300 只港股 1 周数据）
SUBSCRIBE_LIMIT = 300


def fetch_stock_quote(progress, ctx, codes, type, outputdir):
    """批量抓取一批股票的 K 线行情（每只股票一个 parquet 文件）。

    抓取口径：按 codes 顺序、每批 SUBSCRIBE_LIMIT 只处理。一批内目标
    parquet 全部已存在时整批跳过（断点续传）；否则先 unsubscribe_all
    清空旧订阅，再 subscribe 该批（subscribe_push=False，
    Session.ALL），随后在 keep_one_minute() 保护下逐只 _dump 拉取
    （富途 K 线订阅有额度消耗，需限速）。全部批次结束后再
    unsubscribe_all 一次。

    已知现状：订阅或取消订阅失败（ret 非 RET_OK）时打印错误并直接
    return 中止，不做重试；单只 _dump 失败（get_cur_kline 非 RET_OK）
    仅打印错误继续下一只，不重试——该只股票会缺失对应 parquet。

    Args:
        progress: 进度条对象（MultiThreadProgress 或 rich Progress，
            均提供 add_task / update 方法）。
        ctx (futu.OpenQuoteContext): open_quote_context() 返回的行情
            上下文。
        codes (list[str]): 股票代码列表（Futu 格式，如 SH.600000）。
        type (str): 周期符号，取值见模块级 sub_types / kl_types
            （'d'/'w'/'1'/'3'/'5'）。
        outputdir (pathlib.Path): 输出目录；每只股票写 {code}.parquet。

    Returns:
        None: 无返回值；结果直接落盘到 outputdir 下的 parquet 文件。
    """
    task = progress.add_task("Working...", total=len(codes))

    for i in range(0, len(codes), SUBSCRIBE_LIMIT):
        part = codes[i:i+SUBSCRIBE_LIMIT]

        all_exist = True
        for code in part:
            if not (outputdir / f'{code}.parquet').exists():
                all_exist = False
                break
        if all_exist:
            progress.update(task,
                            advance=len(part),
                            description='Skip exist')
            continue

        ret, err = ctx.unsubscribe_all()
        if ret != RET_OK:
            console.error(f'unsubscribe failed, {err}')
            return

        ret, err = ctx.subscribe(
                part,
                [sub_types[type]],
                subscribe_push=False,
                session=Session.ALL)
        if ret != RET_OK:
            console.error(f'subscribe failed, {err}')
            return

        with keep_one_minute():
            for code in part:
                _dump(ctx, outputdir / f'{code}.parquet', code, type)
                progress.update(task,
                                advance=1,
                                description=f'{type}q {code}')

    ret, err = ctx.unsubscribe_all()
    if ret != RET_OK:
        console.error(f'unsubscribe failed, {err}')


def _dump(ctx, path, code, type):
    """拉取单只股票最近 1000 根 K 线并写 parquet（前复权 QFQ）。

    行情口径：get_cur_kline(code, 1000, kl_types[type], AuType.QFQ)，
    QFQ 为前复权，保证历史价格口径连续可回测；失败（ret 非 RET_OK）时
    打印错误返回，不写文件。字段统一改为短名，与后续回测/实盘模块的
    列约定一致：

    - time_key 改名为 t（解析为 datetime 对象）
    - open/close/high/low/volume 改名为 o/c/h/l/v
    - turnover 改名为 a，turnover_rate 改名为 tr 并把小数
      （如 0.0123）转换为百分比（1.23）
    - pe_ratio 改名为 pe，last_close 改名为 lc

    Args:
        ctx (futu.OpenQuoteContext): 行情上下文。
        path (pathlib.Path): 输出 parquet 路径（{code}.parquet）。
        code (str): 股票代码（Futu 格式）。
        type (str): 周期符号，见模块级 kl_types。

    Returns:
        None: 无返回值；成功时结果写入 path。
    """
    if path.exists():
        return

    # QFQ = 前复权，保证历史价格口径连续可回测
    ret, data = ctx.get_cur_kline(code, 1000, kl_types[type], AuType.QFQ)
    if ret != RET_OK:
        console.error(data)
        return

    df = data.rename(columns={
        'time_key': 't',
        'open': 'o',
        'close': 'c',
        'high': 'h',
        'low': 'l',
        'volume': 'v',
        'turnover': 'a',
        'turnover_rate': 'tr',
        'pe_ratio': 'pe',
        'last_close': 'lc',
        })
    df['t'] = df['t'].apply(lambda x: datetime.strptime(x, '%Y-%m-%d %H:%M:%S'))
    df['tr'] = df['tr'].apply(lambda x: x * 100)

    df.to_parquet(path, index=False)
