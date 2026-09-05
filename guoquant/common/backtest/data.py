"""回测数据装载模块

以 ``data_root`` （``out/<日期>/fetched_data``）为根，提供回测所需的数据装载
与 backtrader feed 转换工具：指数行情装载（``load_all_industry_index_dfs`` /
``load_index_df``）、backtrader feed（``make_bt_feed``）与交易日并集
（``get_all_trading_dates``），被 ``backtest/runner.py``、``ic_analyzer.py``
引用。

分工约定（装载实现不在此复刻）：

- 行情/资金流/筹码快照整表加载统一在 ``guoquant.common.quote``
  （``load_quote_dfs`` / ``load_flow_dfs`` / ``load_dist_records``），由
  runner / ic_analysis 命令层直接调用；单文件行情加载 ``load_quote_df`` 在
  本模块 re-export，供本模块的指数装载使用；
- 行业映射 re-export 自 ``guoquant.common.data_path`` （``load_industry_map``，
  供 runner 引用）；目录结构与路径枚举约定见 data_path 模块 docstring。

筹码快照前视偏差说明：``dist/`` 下是筹码分布“最新快照”（无历史时序），在
历史任一日引用都会引入前视偏差（仅 needs_flow 类策略打分使用）；该约定亦见于
``guoquant.common.quote`` 的 ``load_dist_records`` / ``make_quote`` 相关说明。
"""
import pandas as pd
import backtrader as bt
from pathlib import Path

# 单文件行情加载（parquet/json 自动识别 → datetime 索引）统一在
# common.quote.load_quote_df；行业映射 re-export 自 common.data_path。
from guoquant.common.quote import load_quote_df
from guoquant.common.data_path import load_industry_map

def make_bt_feed(code, df, start_date=None, end_date=None):
    """将行情 DataFrame 转换为 backtrader PandasData feed。

    ``df`` 需以 datetime 为索引并含 ``o`` / ``h`` / ``l`` / ``c`` / ``v`` 列，
    转换时重命名为 backtrader 所需列名，并补 ``openinterest`` 列填 0（A 股无
    持仓量概念）。只截取回测区间 [``start_date``, ``end_date``] 内的数据以
    减少 feed 负担。订单在策略 ``next()`` 中发出后，backtrader 于下一根 bar
    的开盘价撮合，因此该 feed 不会引入“当日收盘信号当日成交”的前视偏差。

    Args:
        code (str): 股票代码，写入 feed 的 ``_name`` 供策略识别数据源。
        df (pd.DataFrame): 行情 DataFrame（datetime 索引，含 o/h/l/c/v 列）。
        start_date (date | None): 回测起始日期（含）；默认 None（不限）。
        end_date (date | None): 回测截止日期（含）；默认 None（不限）。

    Returns:
        bt.feeds.PandasData | None: 转换后的 feed；区间内有效 bar 少于 5 根时
        返回 None（数据点太少，backtrader 无法稳定运行）。
    """
    feed_df = df[['o', 'h', 'l', 'c', 'v']].copy()
    feed_df.columns = ['open', 'high', 'low', 'close', 'volume']
    feed_df['openinterest'] = 0.0

    if start_date is not None:
        feed_df = feed_df[feed_df.index.date >= start_date]
    if end_date is not None:
        feed_df = feed_df[feed_df.index.date <= end_date]

    if len(feed_df) < 5:
        # 数据点太少，backtrader 无法稳定运行，视为无有效 feed
        return None

    feed = bt.feeds.PandasData(
        dataname=feed_df,
        datetime=None,  # None 表示使用 DataFrame 索引作为时间轴
        open='open',
        high='high',
        low='low',
        close='close',
        volume='volume',
        openinterest='openinterest',
    )
    feed._name = code
    return feed


def load_all_industry_index_dfs(data_root):
    """加载全部行业板块指数行情数据（``SH.LIST*.parquet``）。

    扫描 ``data_root/quote/d/index/`` 目录，只加载文件名以 ``SH.LIST`` 开头且
    后缀为 ``.parquet`` 的文件；加载失败或结果为空时跳过该文件。

    Args:
        data_root (str | Path): 数据根目录。

    Returns:
        dict[str, pd.DataFrame]: 行业指数代码（文件名 stem，形如 ``SH.LIST...``
        ，与行业板块文件的 ``plate.code`` 对应）→ 行情 DataFrame，用于行业
        轮动动量计算（见 ``scorer._compute_sector_momentum``）。
    """
    index_dir = Path(data_root) / 'quote' / 'd' / 'index'
    result = {}
    if not index_dir.exists():
        return result
    for fpath in index_dir.iterdir():
        if not fpath.stem.startswith('SH.LIST') or fpath.suffix != '.parquet':
            continue
        try:
            df = load_quote_df(fpath)
            if df is not None and len(df) > 0:
                result[fpath.stem] = df
        except Exception:
            pass
    return result


def load_index_df(data_root, index_code='SH.000300'):
    """加载大盘指数行情数据（沪深300 / 中证1000 等）。

    文件路径为 ``data_root/quote/d/index/{index_code}.parquet``。用途：在
    ``runner.py`` 中计算指数相对 MA60 的偏离度，生成渐进式市场择时信号。

    Args:
        data_root (str | Path): 数据根目录。
        index_code (str): 指数代码，默认 ``'SH.000300'``。

    Returns:
        pd.DataFrame | None: 指数行情（datetime 索引，含 o/h/l/c/v 列）；文件
        不存在或加载失败时返回 None。
    """
    path = Path(data_root) / 'quote' / 'd' / 'index' / f'{index_code}.parquet'
    if not path.exists():
        return None
    try:
        return load_quote_df(path)
    except Exception:
        return None


def get_all_trading_dates(quote_dfs, start_date=None, end_date=None):
    """汇总全部行情 DataFrame 的交易日并集，返回排序去重后的日期列表。

    注意这是“并集”而非严格共同交易日：某天只要任意一只股票有数据即被包含，
    用于确定换仓日，避免因个别股票停牌导致整日缺失。返回日期为 ``date``
    对象（仅年月日，忽略盘中时间），可再按 ``start_date`` / ``end_date``
    过滤。

    Args:
        quote_dfs (dict[str, pd.DataFrame]): 各股票行情，须为 datetime 索引。
        start_date (date | None): 过滤下界（含）；默认 None（不限）。
        end_date (date | None): 过滤上界（含）；默认 None（不限）。

    Returns:
        list[date]: 升序去重后的交易日列表。
    """
    all_dates = set()
    for df in quote_dfs.values():
        dates = df.index.date
        all_dates.update(dates)

    all_dates = sorted(all_dates)
    if start_date:
        all_dates = [d for d in all_dates if d >= start_date]
    if end_date:
        all_dates = [d for d in all_dates if d <= end_date]
    return all_dates
