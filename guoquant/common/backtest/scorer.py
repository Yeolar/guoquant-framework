"""回测打分编排模块（引擎侧）

负责回测引擎的打分编排与引擎专用预计算：``precompute_scores`` 按注册表策略名
对所有换仓日并行预计算打分；``_compute_sector_momentum`` /
``_compute_cs_vol_exposure`` 是行业轮动动量、横截面波动率暴露两个引擎专用
预计算因子（runner 使用）。

分工约定（避免 strategies 与回测引擎的依赖环）：

- 行情整表读取与 df→Quote 组装统一在 ``guoquant.common.quote``
  （``load_quote_df`` / ``make_quote`` / ``slice_to_date``），通用打分执行在
  ``common/scoring.py`` （``compute_generic_scores`` / ``compute_flow_scores``
  / ``_estimate_market_value``）；
- 策略模块（``strategies/*.py``）只定义 quotes 级打分核心，由
  ``common/strategy.register_strategy`` 按 ``need_mv`` / ``has_theme`` /
  ``needs_flow`` 配置自动生成引擎打分入口（内部调用 common/scoring 的通用
  执行）；本模块的 ``precompute_scores`` 仅从注册表取引擎入口并按换仓日并行
  编排；
- 策略注册表（``registered_strategies`` / ``get_strategy_names``）位于
  ``guoquant.common.strategy``；本模块顶部不 import strategies 包，各
  ``strategies/*.py`` 内的 ``@register_strategy`` 注册在导入
  ``guoquant.strategies`` 时完成，由引擎入口显式触发（runner 顶部、
  ``precompute_scores`` 内部、实盘 ``common/trade/scorer.py``）。

关键约定：

- 打分只使用截止 ``end_date`` 的数据（``quote.slice_to_date`` 截断），天然
  无前视偏差；
- 唯一例外是 dist 筹码快照（仅最新值），在 flow 类策略中会引入前视偏差，
  说明见 ``common/quote`` 与本包 ``data.py`` 的模块 docstring。
"""
from guoquant.common.strategy import registered_strategies, get_strategy_names
from guoquant.common.quote import slice_to_date


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 引擎专用预计算因子（runner 使用，见 backtest/runner.py）                  #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

def _compute_sector_momentum(quote_dfs, industry_index_dfs, industry_map, end_date, window=20):
    """计算截面日期各股票的行业轮动动量因子值。

    ``SectorMomentum`` = 行业指数（``window`` 日）涨幅 ×（个股相对强度 - 1），
    其中相对强度 = (1 + 个股涨幅) / (1 + 行业涨幅)，大于 1 表示跑赢行业；
    结果裁剪到 [-0.1, 0.1]，避免极端值主导打分。行业或个股截止 ``end_date``
    的历史不足 ``window`` 个交易日时跳过该股票；无任何行业可算时返回空 dict。
    只使用截止 ``end_date`` 的数据（``slice_to_date`` 截断），无前视偏差。

    Args:
        quote_dfs (dict[str, pd.DataFrame]): 各股票行情（datetime 索引，含
            ``c`` 列）。
        industry_index_dfs (dict[str, pd.DataFrame]): 各行业指数行情（datetime
            索引，含 ``c`` 列），键为行业指数代码。
        industry_map (dict[str, str]): 股票代码 → 行业指数代码映射。
        end_date (date): 截面截止日期，只使用该日及之前的数据。
        window (int): 动量计算窗口（交易日数），默认 20。

    Returns:
        dict[str, float]: 股票代码 → 行业轮动动量值（裁剪到 [-0.1, 0.1]）。
    """
    import numpy as np

    # 1. 计算各行业指数 window 日涨幅
    industry_returns = {}
    for ind_code, idx_df in industry_index_dfs.items():
        sub = slice_to_date(idx_df, end_date)
        if sub is None or len(sub) < window:
            continue
        start_price = sub.iloc[-window]['c']
        end_price = sub.iloc[-1]['c']
        if start_price <= 0:
            continue
        industry_returns[ind_code] = end_price / start_price - 1

    if not industry_returns:
        return {}

    # 2. 计算每个股票在行业内的相对强度
    stock_sm = {}
    for code, df in quote_dfs.items():
        ind = industry_map.get(code)
        if ind is None or ind not in industry_returns:
            continue
        sub = slice_to_date(df, end_date)
        if sub is None or len(sub) < window:
            continue
        stock_ret = sub.iloc[-1]['c'] / sub.iloc[-window]['c'] - 1
        ind_ret = industry_returns[ind]

        # 相对强度: (1+个股涨幅) / (1+行业涨幅)，>1 表示跑赢行业
        rs = (1 + stock_ret) / (1 + ind_ret) if (1 + ind_ret) > 0 else 1.0

        # 行业动量 = 行业涨幅 × (相对强度偏离 - 1)
        # 正值 = 行业涨 + 个股跑赢行业（行业景气 + 个股强势，双重加成）
        sm_value = ind_ret * (rs - 1)
        # 限制在合理范围 [-0.1, 0.1]，避免极端值主导打分
        stock_sm[code] = float(np.clip(sm_value, -0.1, 0.1))

    return stock_sm


def _compute_cs_vol_exposure(quote_dfs, rebalance_dates, lookback=60):
    """计算各换仓日的横截面波动率自适应仓位暴露因子。

    ``CS_Vol`` 为当日全部股票单日收益率的横截面标准差（有效样本不足 30 只的
    日期暴露按 1.0 处理）。暴露按以下公式计算，其中 ``CS_Vol_avg`` 为截至
    当日（含）最近 ``lookback`` 个截面的 CS_Vol 均值（有效样本不足 5 个时
    暴露按 1.0）：

    ``exposure = 1 / (1 + 10 × max(0, CS_Vol - CS_Vol_avg))``

    含义：横截面波动率显著高于常态水平时说明市场进入高波动状态、应降低仓位，
    波动率回落后恢复满仓。暴露裁剪到 [0.3, 1.0]，与市场择时信号相乘得到最终
    仓位倍数。只使用各截面当日及之前的数据，无前视偏差。

    Args:
        quote_dfs (dict[str, pd.DataFrame]): 各股票行情（datetime 索引，含
            ``c`` 列）。
        rebalance_dates (list[date]): 需要计算暴露的换仓日序列。
        lookback (int): 常态波动率基准的回看截面数，默认 60。

    Returns:
        dict[date, float]: 换仓日 → 仓位暴露倍数（裁剪到 [0.3, 1.0]）。
    """
    import numpy as np

    cs_vol_by_date = {}
    for d in rebalance_dates:
        rets = []
        for code, df in quote_dfs.items():
            sub = slice_to_date(df, d)
            if sub is None or len(sub) < 2:
                continue
            if sub.iloc[-1]['c'] <= 0 or sub.iloc[-2]['c'] <= 0:
                continue
            # 当日单日收益率：只用到 d 及之前数据，无前视偏差
            ret = sub.iloc[-1]['c'] / sub.iloc[-2]['c'] - 1
            rets.append(ret)

        # 样本太少时横截面标准差不可靠，标记为 None 稍后按 1.0 处理
        if len(rets) >= 30:
            cs_vol_by_date[d] = float(np.std(rets))
        else:
            cs_vol_by_date[d] = None

    dates = sorted(cs_vol_by_date.keys())
    exposure_by_date = {}
    for i, d in enumerate(dates):
        if cs_vol_by_date[d] is None:
            exposure_by_date[d] = 1.0
            continue

        # 取截至当日（含）lookback 个截面的 CS_Vol，作为"常态波动率"基准
        window_vals = [cs_vol_by_date[dates[j]]
                       for j in range(max(0, i - lookback), i + 1)
                       if cs_vol_by_date[dates[j]] is not None]
        if len(window_vals) < 5:
            exposure_by_date[d] = 1.0
            continue

        avg_vol = np.mean(window_vals)
        cur_vol = cs_vol_by_date[d]
        # 只有当前波动率高于基准时才降仓；10 为敏感度系数
        exposure = 1.0 / (1.0 + 10.0 * max(0, cur_vol - avg_vol))
        exposure_by_date[d] = float(np.clip(exposure, 0.3, 1.0))

    return exposure_by_date


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 预计算入口                                                               #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

def precompute_scores(quote_dfs, rebalance_dates, strategy_name,
                      flow_dfs=None, dist_records=None, verbose_cb=None,
                      sector_momentum_by_date=None, max_workers=4):
    """对所有换仓日预计算打分，返回 ``{date: {code: score}}``。

    从注册表取 ``strategy_name`` 对应的引擎打分入口，逐换仓日调用并按
    ``max_workers`` 并行编排；``max_workers <= 1`` 或换仓日不足 2 个时自动走
    串行路径（进度回调加锁，避免多线程输出交错）。本模块不静态 import
    strategies 包，直接调用本函数时会先导入 ``guoquant.strategies`` 以触发各
    策略模块内的 ``@register_strategy`` 注册。

    Args:
        quote_dfs (dict[str, pd.DataFrame]): 各股票行情。
        rebalance_dates (list[date]): 换仓日序列，其元素也是返回 dict 的键。
        strategy_name (str): 注册表内策略名，可用集合见
            ``get_strategy_names()``。
        flow_dfs (dict[str, pd.DataFrame] | None): 资金流数据，flow 类策略
            需要；默认 None。
        dist_records (object | None): 筹码快照数据（含 dist 快照的策略需要，
            格式见 ``common.quote.load_dist_records``）；默认 None。
        verbose_cb (callable | None): 进度回调，签名
            ``verbose_cb(date, n_scored)``；默认 None。
        sector_momentum_by_date (dict[date, dict[str, float]] | None): 行业轮动
            动量（``_compute_sector_momentum`` 的输出），无对应日期数据时打分
            内部退化为默认值；默认 None。
        max_workers (int): 并行线程数，默认 4。

    Returns:
        dict[date, dict[str, float]]: 换仓日 → {股票代码: 打分}。

    Raises:
        ValueError: ``strategy_name`` 不在注册表中时。
    """
    # 触发全部策略注册（strategies/*.py 模块内 @register_strategy）：
    # 本模块不静态 import strategies，直接调用本函数时在此完成注册
    import guoquant.strategies  # noqa: F401

    if strategy_name not in registered_strategies:
        raise ValueError(
            f'未知策略: {strategy_name}，可用: {get_strategy_names()}')

    compute_fn, needs_flow = registered_strategies[strategy_name]

    def _score_date(d):
        # 各换仓日的行业动量可能不同；无该日数据时为 None，
        # 打分函数内部会退化为默认值（sector_momentum or {}）
        sm = sector_momentum_by_date.get(d) if sector_momentum_by_date else None
        scores = compute_fn(quote_dfs, d, flow_dfs, dist_records,
                           sector_momentum=sm)
        return d, scores

    if max_workers <= 1 or len(rebalance_dates) <= 1:
        # 串行路径
        scores_by_date = {}
        for d in rebalance_dates:
            _, scores = _score_date(d)
            scores_by_date[d] = scores
            if verbose_cb:
                verbose_cb(d, len(scores))
        return scores_by_date

    # 并行路径：ThreadPoolExecutor（pandas/numpy 操作释放 GIL）
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    scores_by_date = {}
    cb_lock = threading.Lock()  # 进度回调需加锁，避免多线程下输出交错

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_date = {
            executor.submit(_score_date, d): d
            for d in rebalance_dates
        }
        for future in as_completed(future_to_date):
            d, scores = future.result()
            scores_by_date[d] = scores
            if verbose_cb:
                with cb_lock:
                    verbose_cb(d, len(scores))

    return scores_by_date
