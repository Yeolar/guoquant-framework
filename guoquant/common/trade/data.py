"""
实盘数据加载模块（QMT 实时快照侧）。

历史行情整表与行业映射的读取统一走 common 层（``quote.load_quote_dfs`` /
``data_path.load_industry_map``，parquet/json 自动识别），本模块只保留实盘
特有的部分：

- ``load_filtered_stocks``：市值分层白名单股票池 CSV → ``{code}`` 集合（供
  候选范围限制）；
- ``fetch_latest_quote``：QMT 当日实时行情快照（最新一根 K 线）；
- ``build_quote`` / ``build_all_quotes``：“历史 + 当日快照”拼接构建 Quote，
  使实时数据进入打分序列的末位；
- ``load_industry_map``：由 ``data_path`` 再导出（``auto_trade`` 从本模块统一
  导入）。

被 ``commands/auto_trade.py`` 引用（``load_filtered_stocks`` /
``load_industry_map`` / ``fetch_latest_quote`` / ``build_all_quotes``；
历史整表加载直接用 ``quote.load_quote_dfs``）。
"""
import pandas as pd
from pathlib import Path
from datetime import date

from guoquant.common.data_path import filtered_stock_paths, load_industry_map
from guoquant.common.quote import Quote
from guoquant.common.fetch.qmtapi.context import to_qmt_code, open_quote_context


def load_filtered_stocks(data_root):
    """
    加载市值分层白名单股票池，返回全部档位的股票代码集合。

    读取 ``stockfiltered`` 目录（与数据根同级）下各市值档的 CSV，文件名形如
    ``stock_mv_{10|30|100|300}.csv``，由 ``fetch_stockfiltered`` 命令生成。
    CSV 无表头、每行 ``code, name, 流通市值（元）`` （rank 等消费方按位置解析，
    故不能加表头），因此以 ``header=None`` 显式读取；文件缺失或解析失败的档位
    直接跳过。

    Args:
        data_root (str | Path): 数据根目录（``fetched_data``，其上级目录即
            ``stockfiltered`` 所在位置）。

    Returns:
        set: 去重后的股票代码集合（空文件/空目录时为 set()）。
    """
    paths = filtered_stock_paths(Path(data_root))
    codes = set()
    for p in paths:
        if not p.exists():
            continue
        try:
            # CSV 无表头（每行 "code, name, 市值(元)"，由 fetch_stockfiltered
            # 命令生成；rank 等消费方按位置解析，故不能加表头）→ 显式 header=None
            df = pd.read_csv(p, header=None)
            codes.update(df[0].astype(str).str.strip().tolist())
        except Exception:
            pass
    return codes


def fetch_latest_quote(codes, period='1d'):
    """
    通过 QMT xtdata 获取各股票当日（最新）行情快照。

    口径说明：

    - ``get_market_data_ex`` 返回“字段名 → DataFrame（行=股票，列=时间）”的
      字典，因此取最后一列（最新时间）作为当日快照；
    - 以 ``dividend_type='front'`` 前复权取价，与历史 K 线保持同一复权口径，
      便于与历史序列直接拼接；
    - QMT 返回的 ``turnover_rate`` 为小数（如 0.0123），此处统一换算为
      百分比（%）；无该字段时结果中不含 ``tr`` 键。

    Args:
        codes (list): 股票代码列表（内部经 ``to_qmt_code`` 转 QMT 格式后查询）。
        period (str): K 线周期，默认 ``'1d'`` （日线）。

    Returns:
        dict: 代码 → 当日快照字段（c / o / h / l / v 分别为收盘 / 开盘 / 最高 /
        最低 / 成交量，可选的 tr 为换手率百分比，仅 QMT 返回换手率时出现）；
        取不到行情的股票不在结果中。
    """
    result = {}
    if not codes:
        return result

    with open_quote_context() as ctx:
        qmt_codes = [to_qmt_code(c) for c in codes]
        ctx.download_history_data2(qmt_codes, period, start_time='',
                                   end_time='')

        for code in codes:
            qmt_code = to_qmt_code(code)
            try:
                kline = ctx.get_market_data_ex(
                    field_list=[],
                    stock_list=[qmt_code],
                    period=period,
                    start_time='',
                    end_time='',
                    count=1,
                    dividend_type='front',
                    fill_data=True,
                )
                if not kline or qmt_code not in kline:
                    continue

                # close 为 DataFrame，列=时间（get_market_data_ex 返回的布局见函数 docstring），
                # 取最后一列作为最新时间
                close_s = kline.get('close', pd.DataFrame())
                if close_s.empty or qmt_code not in close_s.index:
                    continue

                last_ts = close_s.columns[-1]
                row = {
                    'c': float(close_s.loc[qmt_code, last_ts]),
                    'o': float(kline.get('open', close_s).loc[qmt_code, last_ts]),
                    'h': float(kline.get('high', close_s).loc[qmt_code, last_ts]),
                    'l': float(kline.get('low', close_s).loc[qmt_code, last_ts]),
                    'v': float(kline.get('volume', close_s).loc[qmt_code, last_ts]),
                }
                # turnover_rate 在 QMT 中是小数，转百分比
                if 'turnover_rate' in kline:
                    tr_val = kline['turnover_rate'].loc[qmt_code, last_ts]
                    row['tr'] = float(tr_val) * 100 if pd.notna(tr_val) else None

                result[code] = row
            except Exception:
                pass

    return result


def build_quote(code, hist_df, latest_snapshot=None):
    """
    拼接历史行情与当日快照，构建单个 Quote 对象。

    拼接方式：把当日快照视为对历史末根 K 线的刷新（约定末根即“今天”的
    未收盘 K 线），原地更新该行而不新增行——close / open / volume / tr 取
    快照值，high 取历史与快照的较大值、low 取较小值（当日区间并集），并把
    时间戳替换为当前系统时间。由此把“当日实时”并入历史序列的末位，保证后续
    打分看到的永远是连续完整序列，且不引入未来数据；内部同时把时间列换算为
    Unix 秒（``Quote.load_quote`` 的时间语义）。

    Args:
        code (str): 股票代码（Quote 标识）。
        hist_df (pandas.DataFrame): 历史行情 DataFrame（datetime 索引、含 K 线
            各列及保留的 ``t`` 列，通常来自 ``quote.load_quote_dfs``）。
        latest_snapshot (dict, optional): 当日最新快照（来自
            ``fetch_latest_quote``）；为 None 时仅做时间列换算、不更新末根。

    Returns:
        Quote | None: 构建的 Quote 对象；``hist_df`` 为空时返回 None。
    """
    if hist_df is None or len(hist_df) == 0:
        return None

    # datetime 索引复位为普通行，t 列换算成 unix 秒（Quote.load_quote 的时间语义）
    df = hist_df.reset_index()
    df['t'] = (pd.to_datetime(df['t']).astype('int64') // 1e9).astype(int)

    # 有当日快照时：原地更新末根（当日）K 线，不新增行
    if latest_snapshot is not None:
        last_row = df.iloc[-1].to_dict()
        last_row['c'] = latest_snapshot.get('c', last_row.get('c'))
        last_row['o'] = latest_snapshot.get('o', last_row.get('o'))
        # high 取历史与快照较大值、low 取较小值（当日区间并集）
        last_row['h'] = max(last_row.get('h', 0), latest_snapshot.get('h', 0))
        last_row['l'] = min(last_row.get('l', float('inf')), latest_snapshot.get('l', float('inf')))
        last_row['v'] = latest_snapshot.get('v', last_row.get('v'))
        if 'tr' in latest_snapshot and latest_snapshot['tr'] is not None:
            last_row['tr'] = latest_snapshot['tr']
        # 时间戳刷新为当前时刻，使该行作为序列最新一期
        import time
        last_row['t'] = int(time.time())
        df.iloc[-1] = last_row

    quote = Quote(code)
    quote.load_quote(df)
    return quote


def build_all_quotes(quote_dfs, latest_quotes=None, filtered_codes=None):
    """
    批量构建全市场 Quote 对象。

    候选代码取 ``quote_dfs`` 的键与 ``filtered_codes`` 的交集（后者为 None 时
    用全部键），逐只调用 ``build_quote`` 拼接当日快照；构建结果为空或 K 线
    不足 20 根的股票被跳过。

    Args:
        quote_dfs (dict): ``{code: DataFrame}`` 历史行情（通常来自
            ``quote.load_quote_dfs``）。
        latest_quotes (dict, optional): ``{code: snapshot_dict}`` 当日快照，
            默认 None。
        filtered_codes (set, optional): 限定股票范围，与 ``quote_dfs`` 键求
            交集；默认 None（不限制）。

    Returns:
        dict: ``{code: Quote}``；K 线不足 20 根的股票不在结果中。
    """
    quotes = {}
    codes = set(quote_dfs.keys())
    if filtered_codes:
        codes = codes & filtered_codes

    for code in codes:
        snap = latest_quotes.get(code) if latest_quotes else None
        q = build_quote(code, quote_dfs[code], snap)
        if q is not None and len(q) >= 20:
            quotes[code] = q

    return quotes
