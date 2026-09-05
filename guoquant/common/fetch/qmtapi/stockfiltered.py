"""
股票筛选抓取（QMT）——按流通市值过滤全市场股票并落盘 CSV（本地筛选）。

职责：作为 qmt provider 的一部分，由 commands/fetch_stockfiltered.py
调用；输出与 futu provider 产物同构的 CSV（无表头，每行
"code, name, 市值(元)"），供 guoquant.common.trade.data 与 data_path
按市值分层读取（与 futu 版产出格式一致）。

xtdata 无服务端筛选接口（futu get_stock_filter 的等价物），这里用本地
数据组装，流程如下：

1. get_stock_list_in_sector('沪深A股') 取全市场股票列表（板块名随 QMT
   版本有差异，按候选列表逐个尝试），再剔除北交所/指数等非 .SH/.SZ 标的；
2. get_market_data 一次批量取全市场日线收盘价（本地数据）；
3. 逐只 get_instrument_detail 取流通股本与名称（本地文件查询），计算
   流通市值 = 收盘价 × 流通股本，并剔除名称含 'ST' 的股票；
4. 按市值区间 [min_mv, max_mv]（亿元）过滤后写 CSV。

其他约定与已知现状：

- get_instrument_detail 的字段名随 QMT 客户端版本可能有差异
  （FloatShare / float_share 等），实现里做了多键容错；
- 全市场逐只查询耗时取决于客户端本地缓存，进度条可见；
- ctx 参数仅用于与 futu 版签名对齐，xtdata 无需连接上下文。
"""
from guoquant.common.log import console
from xtquant import xtdata

# 全市场板块名（不同 QMT 版本/客户端名称有差异，逐个尝试）
_MARKET_SECTORS = ('沪深A股', '沪深京A股', '全部A股', '沪深股票')


def _qmt_to_futu(qmt_code):
    """QMT 代码（600000.SH）转 Futu 代码（SH.600000）。

    转换口径：若代码含 '.'，按最后一个 '.' 拆分为 代码.市场 再调换
    顺序拼接（如 600000.SH 转 SH.600000）；不含 '.' 的代码原样返回。

    Args:
        qmt_code (str): QMT 格式代码，如 ``600000.SH``。

    Returns:
        str: Futu 格式代码（如 ``SH.600000``）；不含 '.' 时原样返回。
    """
    if '.' in qmt_code:
        code, mkt = qmt_code.rsplit('.', 1)
        return f'{mkt}.{code}'
    return qmt_code


def _get_float_share(detail):
    """从 get_instrument_detail 返回值中容错提取流通股本（股）。

    依次尝试 FloatShare / float_share / FLOAT_SHARE / FreeShare 键，
    取第一个非空值并转为 float；返回的是流通股本（股），乘以收盘价即
    流通市值（元）。

    Args:
        detail (dict): get_instrument_detail 的返回字典。

    Returns:
        float: 流通股本（股）；四个候选键均缺失或为空时返回 None。
    """
    for key in ('FloatShare', 'float_share', 'FLOAT_SHARE', 'FreeShare'):
        v = detail.get(key)
        if v:
            return float(v)
    return None


def _get_name(detail):
    """从 get_instrument_detail 返回值中容错提取股票名称。

    依次尝试 InstrumentName / instrument_name / StockName / stock_name
    键，取第一个非空值并转 str。

    Args:
        detail (dict): get_instrument_detail 的返回字典。

    Returns:
        str: 股票名称；全部候选键缺失或为空时返回空字符串 ''。
    """
    for key in ('InstrumentName', 'instrument_name', 'StockName', 'stock_name'):
        v = detail.get(key)
        if v:
            return str(v)
    return ''


def fetch_stock_filtered(progress, ctx, output, min_mv, max_mv=None):
    """按流通市值区间在本地筛选全市场 A 股，过滤 ST 股后写 CSV。

    筛选口径（本地实现，无服务端筛选接口）：

    - 全市场股票取自 get_stock_list_in_sector（_MARKET_SECTORS 候选
      板块名逐个尝试，取首个非空结果），再剔除北交所/指数等非 .SH/.SZ
      后缀标的；
    - 收盘价取 get_market_data 批量日线最后一根（本地缓存数据）；
    - 流通市值 = 收盘价 × 流通股本（get_instrument_detail 多键容错提取）；
    - 名称含 'ST'、缺收盘价/股本、或市值 NaN（停牌/无数据）的股票剔除；
    - min_mv / max_mv 单位为亿元，按 [min_mv, max_mv] 过滤后写出。

    输出 CSV 无表头，每行 "code, name, value"：code 为 Futu 格式
    （SH.600000，经 _qmt_to_futu 转换），value 为流通市值（元，
    与 futu 版 FLOAT_MARKET_VAL 口径一致）。

    已知现状：未取到全市场股票列表或 get_market_data 失败时打印错误并
    return，不产生输出文件；板块名、字段名随 QMT 客户端版本可能有差异
    （见模块 docstring 的容错说明）。

    Args:
        progress: 进度条对象（MultiThreadProgress 或 rich Progress）。
        ctx: 与 futu 版签名对齐的参数（xtdata 为模块级调用，未使用）。
        output (str): 输出 CSV 文件路径。
        min_mv (float): 市值下限（亿元）。
        max_mv (float, optional): 市值上限（亿元）；为 None 时不限上限。

    Returns:
        None: 无返回值；成功时结果直接写入 output 文件。
    """
    # 1. 全市场股票列表（板块名逐个尝试）
    stock_list = []
    for sector in _MARKET_SECTORS:
        try:
            stock_list = xtdata.get_stock_list_in_sector(sector)
            if stock_list:
                break
        except Exception as e:
            console.error(f'{sector}: {e}')
    if not stock_list:
        console.error('未获取到全市场股票列表（板块名不匹配？）')
        return
    # 仅保留沪深 A 股（剔除指数/基金/北交所等）
    stock_list = [c for c in stock_list
                  if c.endswith('.SH') or c.endswith('.SZ')]

    # 2. 批量日线收盘价（一次调用）
    task = progress.add_task("Working...", total=len(stock_list))
    try:
        close_df = xtdata.get_market_data(
            ['close'], stock_list, period='1d', start_time='', end_time='')
    except Exception as e:
        console.error(f'get_market_data failed: {e}')
        return
    closes = close_df['close'].iloc[-1] if close_df is not None and len(close_df) else {}

    # 3-4. 逐只查股本/名称 → 市值过滤 + 排除 ST → 写 CSV
    lines = []
    for qmt_code in stock_list:
        try:
            detail = xtdata.get_instrument_detail(qmt_code)
        except Exception as e:
            console.error(f'{qmt_code}: {e}')
            progress.update(task, advance=1)
            continue

        name = _get_name(detail)
        if 'ST' in name:
            progress.update(task, advance=1)
            continue

        close = closes.get(qmt_code)
        float_share = _get_float_share(detail)
        if close is None or float_share is None:
            progress.update(task, advance=1)
            continue

        mv = close * float_share  # 流通市值（元）
        if mv != mv:  # NaN（停牌/无数据）
            progress.update(task, advance=1)
            continue
        mv_yi = mv / 100000000
        if mv_yi < min_mv or (max_mv is not None and mv_yi > max_mv):
            progress.update(task, advance=1)
            continue

        lines.append(f'{_qmt_to_futu(qmt_code)}, {name}, {mv:.0f}')
        progress.update(task, advance=1)

    with open(output, 'w') as fp:
        fp.write('\n'.join(lines) + '\n')
    console.print(f'filtered {len(lines)} stocks -> {output}')
