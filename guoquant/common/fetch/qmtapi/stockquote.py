"""
K 线行情抓取（QMT）——逐股下载并落盘 parquet，列名对齐 futu provider 产物。

职责：作为 qmt provider 的一部分，由 commands/fetch_stockdata.py 与
commands/fetch_stockquote.py 调用；每只股票一个 parquet 文件，落盘在
fetched_data/quote/{周期}/stock/ 下（与 futu provider 相同的目录约定，
文件名沿用传入的 Futu 格式代码 {code}.parquet）。

产物约定：列统一为短名（t/o/c/h/l/v/a/tr/lc），t 为 datetime、tr 为
百分比（%），与 guoquant.common.quote 的加载约定一致，从而与 futu
provider 产物互换使用。周期符号映射见模块级 period_map（'1'/'3'/'5'
分钟、'd' 日线、'w' 周线），保留字段见模块级 _KLINE_FIELDS。
"""
import pandas as pd
from guoquant.common.log import console
from xtquant import xtdata

from .context import to_qmt_code


# 周期符号 → xtdata period 名：'1'/'3'/'5' 分钟、'd' 日线、'w' 周线
period_map = {
    '1': '1m',
    '3': '3m',
    '5': '5m',
    'd': '1d',
    'w': '1w',
}

# 要保留的 QMT 原始 K 线字段（与 futu get_cur_kline 输出列对应，
# 转置成逐行 K 线后统一改名为短名）
_KLINE_FIELDS = ['open', 'high', 'low', 'close', 'volume', 'amount',
                 'turnover_rate', 'preClose']


def fetch_stock_quote(progress, ctx, codes, qtype, outputdir):
    """批量抓取一批股票的 K 线行情（每只股票一个 parquet 文件）。

    抓取口径：按周期符号取出 period_map[qtype] 后逐只调用 _dump
    （内部先 download_history_data 再 get_market_data）；outputdir 下
    已存在 {code}.parquet（文件名用传入的 Futu 代码）的股票直接跳过
    （断点续传）。本地数据未命中（_dump 内无数据）仅打印错误并继续，
    不做重试。

    Args:
        progress: 进度条对象（MultiThreadProgress 或 rich Progress，
            均提供 add_task / update 方法）。
        ctx: 与 futu 版签名对齐的参数（xtdata 为模块级调用，未使用）。
        codes (list[str]): 股票代码列表（Futu 格式，如 SH.600000；
            内部经 to_qmt_code 转换后下载）。
        qtype (str): 周期符号，取值见模块级 period_map
            （'d'/'w'/'1'/'3'/'5'）。
        outputdir (pathlib.Path): 输出目录；每只股票写 {code}.parquet。

    Returns:
        None: 无返回值；结果直接落盘到 outputdir 下的 parquet 文件。
    """
    task = progress.add_task("Working...", total=len(codes))
    period = period_map[qtype]

    for code in codes:
        outpath = outputdir / f'{code}.parquet'
        if outpath.exists():
            progress.update(task, advance=1, description='Skip exist')
            continue

        _dump(outpath, code, period)
        progress.update(task, advance=1, description=f'{qtype}q {code}')


def _dump(path, code, period):
    """下载单只股票 K 线并转置为逐行格式写 parquet（前复权）。

    行情口径：先把 Futu 代码经 to_qmt_code 转成 QMT 代码，调
    download_history_data 拉取本地数据，再 get_market_data（count=-1
    全量、dividend_type='front' 前复权）读取。xtdata 返回形如
    {字段: DataFrame(行=股票, 列=时间)}，本函数把每个时间点转置为一行
    一根 K 线（与 futu 产物的逐行格式一致），随后：

    - 字段改名为统一短名（见模块 docstring）；
    - 剔除 OHLCV 全部为空的整行（该时间点无行情数据）；
    - 换手率 tr 为小数（如 0.0123），统一 ×100 转百分比（与 futu 产物
      一致），NaN 保留。

    无数据（download 未成功，data 为空或 close 中无该股票）时打印错误
    返回，不写文件。

    Args:
        path (pathlib.Path): 输出 parquet 路径（{code}.parquet，code 为
            Futu 格式）。
        code (str): 股票代码（Futu 格式，函数内转 QMT 格式下载）。
        period (str): xtdata 周期名（period_map 的值，如 '1d'/'1w'）。

    Returns:
        None: 无返回值；成功时结果写入 path。
    """
    if path.exists():
        return

    qmt_code = to_qmt_code(code)
    xtdata.download_history_data(qmt_code, period)
    data = xtdata.get_market_data(
        field_list=[],
        stock_list=[qmt_code],
        period=period,
        count=-1,
        dividend_type='front',
    )

    if not data or qmt_code not in data.get('close', pd.DataFrame()):
        console.error(f'no data for {code}')
        return

    # xtdata 返回 {字段: DataFrame(行=股票, 列=时间)}；这里把每个时间
    # 点转置成一行一根 K 线，与 futu 产物的逐行格式一致
    close_df = data['close']
    times = close_df.columns.tolist()
    rows = []
    for t in times:
        row = {'t': pd.Timestamp(t)}
        for field in _KLINE_FIELDS:
            if field in data:
                val = data[field].loc[qmt_code, t] if qmt_code in data[field].index else None
                row[field] = val
        rows.append(row)

    df = pd.DataFrame(rows).sort_values('t').reset_index(drop=True)

    # 字段映射为 futu 产物的统一短名
    df = df.rename(columns={
        'open': 'o',
        'close': 'c',
        'high': 'h',
        'low': 'l',
        'volume': 'v',
        'amount': 'a',
        'turnover_rate': 'tr',
        'preClose': 'lc',
    })

    # 剔除全部 OHLCV 均为空的整行（该时间点无行情数据）
    df = df.dropna(subset=['o', 'c', 'h', 'l', 'v'], how='all')

    # QMT 换手率为小数（如 0.0123），统一转为百分比（与 futu 产物一致）
    if 'tr' in df.columns:
        df['tr'] = df['tr'].apply(
            lambda x: x * 100 if pd.notna(x) else None)

    df.to_parquet(path, index=False)
