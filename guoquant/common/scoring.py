"""通用策略打分执行（common 层叶子模块）。

负责“通用策略打分执行”：

- ``_estimate_market_value`` ：按换手率/成交额近似估算流通市值；
- ``compute_generic_scores`` / ``compute_flow_scores`` ：把 ``{code: DataFrame}``
  整表按策略元数据需求（``need_mv`` / ``has_theme`` / ``extra_meta_fn`` ）
  组装成 ``[Quote, ...]`` 条目列表，调用策略的 quotes 级打分核心并返回
  ``{code: float}``。

文件 → df 与 df → Quote 组装/按日截断统一在 ``guoquant.common.quote``
（``load_quote_df`` / ``make_quote`` / ``slice_to_date`` ），本模块只做组装
与执行。

被调用方：``guoquant.common.strategy`` 的 ``register_strategy`` 在注册
quotes 级打分核心时按引擎配置（``needs_flow`` / ``need_mv`` /
``has_theme`` ）自动生成适配闭包，回测（``common/backtest/scorer.py`` 的
``precompute_scores`` ）与实盘（``common/trade/scorer.py`` ）经注册表调用
本模块的通用打分执行；市值估算随 ``need_mv=True`` 的策略在此发生，引擎侧
无需直接引用本模块。

设计约束：本模块只依赖 ``guoquant.common.quote`` ，不 import strategies、
不 import 注册表——strategies 依赖 common 层内部件而非回测引擎目录，二者
由此解耦（strategies → common 单向依赖，无环）。引擎编排
（``precompute_scores`` ）与行业动量/横截面波动率预计算因子仍留在
``common/backtest/scorer.py``。
"""
import pandas as pd

from guoquant.common.quote import Quote, make_quote, slice_to_date


# 市值估算兜底值（100 亿，与 stock_strength_strategy 市值偏好的
# 对数高斯中心一致）：换手率数据缺失/无效时返回该值，
# 表示"市值未知，按典型值处理"
_DEFAULT_MARKET_VALUE = 100e8


def _estimate_market_value(df):
    """按换手率与成交额近似估算流通市值。

    估算口径：``tr(%)`` = 成交量 / 流通股本 × 100，``amount`` = 成交量 × 均价
    ≈ 成交量 × close，故流通市值 = ``amount × 100 / tr`` ；取最后 5 日均值以
    减少单日波动影响。该近似为估算（真实流通市值需要股本数据），仅供引擎侧
    ``need_mv=True`` 的策略使用（目前实际参与打分的是 ``stock_strength_strategy``
    的市值维度），不要求精确。

    Args:
        df (pd.DataFrame): 个股K线 DataFrame，需含列 ``tr`` 与 ``a`` ，分别表示
            换手率（%）与成交额；取末 5 行参与估算。

    Returns:
        float: 估算流通市值（元）；换手率全部无效或结果非正时返回兜底值
            ``_DEFAULT_MARKET_VALUE`` ，即 100 亿。
    """
    tail = df.tail(5)
    tr = tail['tr']
    amount = tail['a']
    valid = tr > 0
    if not valid.any():
        return _DEFAULT_MARKET_VALUE
    mv = (amount[valid] * 100 / tr[valid]).mean()
    return mv if mv > 0 else _DEFAULT_MARKET_VALUE


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 通用打分执行（register_strategy 自动生成的引擎适配闭包调用）                        #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

def compute_generic_scores(quote_dfs, end_date, strategy_func,
                           extra_meta_fn=None, need_mv=False, has_theme=True,
                           sector_momentum=None):
    """通用 OHLCV 策略打分执行。

    对 ``{code: DataFrame}`` 整表逐只 ``make_quote`` 组装，按 ``has_theme`` /
    ``need_mv`` / ``extra_meta_fn`` 依次拼接条目 ``[Quote, sector_momentum?, mv?, *extra...]``
    后整体传给 ``strategy_func`` ，再把其返回的 ``(code, score)`` 序列统一转为
    ``{code: float}`` ；条目顺序与各策略函数参数签名（``quotes_with_meta`` 内
    元素）严格对应。本函数由 ``register_strategy`` 自动生成的引擎适配闭包调用。

    Args:
        quote_dfs (dict[str, pd.DataFrame]): 股票代码 → K线 DataFrame。
        end_date (date): 打分截止日（含）。
        strategy_func (callable): 策略模块的 ``xxx_strategy`` 打分函数；所需
            最少K线根数取 ``strategy_func.window`` ，它由 ``@window`` 装饰器声明
            （见 common/strategy.py），数据不足的个股跳过。
        extra_meta_fn (callable | None): 签名 ``(code, df, q, end_date)`` → 额外
            参数列表，追加到条目末尾。默认 None。
        need_mv (bool): 是否附带市值估算（换手率无效时返回兜底值）。默认
            False。
        has_theme (bool): 是否附带 ``sector_momentum`` 项（``rise_rate`` 类
            策略不需要）。默认 True。
        sector_momentum (dict[str, float] | None): 行业轮动动量
            ``{code: float}`` ；无映射的股票默认 0.5。默认 None。

    Returns:
        dict[str, float]: 股票代码 → 得分；无可打分股票时返回空 dict。
    """
    quotes_with_meta = []
    sm = sector_momentum or {}
    for code, df in quote_dfs.items():
        q = make_quote(code, df, end_date)
        if q is None or len(q) < strategy_func.window:
            continue
        entry = [q]
        if has_theme:
            entry.append(sm.get(code, 0.5))  # sector_momentum，无映射时默认 0.5
        if need_mv:
            sub = slice_to_date(df, end_date)
            mv = _estimate_market_value(sub) if sub is not None else _DEFAULT_MARKET_VALUE
            entry.append(mv)
        if extra_meta_fn:
            entry.extend(extra_meta_fn(code, df, q, end_date))
        quotes_with_meta.append(entry)

    if not quotes_with_meta:
        return {}
    result = strategy_func(quotes_with_meta)
    # 策略函数返回 [(code, score), ...]，统一转成 {code: float}
    return {code: float(score) for code, score in result}


def compute_flow_scores(quote_dfs, flow_dfs, dist_records, end_date,
                               strategy_func, sector_momentum=None):
    """通用资金流策略打分执行（需 flow + dist 数据）。

    与 ``compute_generic_scores`` 的区别：条目固定为 ``[Quote, sector_momentum]`` ，
    Quote 经 ``quote.make_quote`` 额外加载资金流与筹码分布；缺失 flow / dist
    数据的股票直接跳过，K线不足 ``strategy_func.window`` 根的个股同样跳过。
    本函数由 ``register_strategy`` 在 ``needs_flow=True`` 时自动生成的引擎适配
    闭包调用。

    Args:
        quote_dfs (dict[str, pd.DataFrame]): 股票代码 → K线 DataFrame。
        flow_dfs (dict[str, pd.DataFrame]): 股票代码 → 资金流 DataFrame。
        dist_records (dict[str, pd.Series | dict]): 股票代码 → 筹码快照最新
            记录。
        end_date (date): 打分截止日（含）。
        strategy_func (callable): 策略模块的 ``xxx_strategy`` 打分函数；所需
            最少K线根数取 ``strategy_func.window``。
        sector_momentum (dict[str, float] | None): 行业轮动动量
            ``{code: float}`` ；无映射的股票默认 0.5。默认 None。

    Returns:
        dict[str, float]: 股票代码 → 得分；无可打分股票时返回空 dict。
    """
    quotes_with_meta = []
    sm = sector_momentum or {}
    for code in quote_dfs:
        if code not in flow_dfs or code not in dist_records:
            continue
        q = make_quote(
            code, quote_dfs[code], end_date,
            flow_df=flow_dfs[code], dist_record=dist_records[code])
        if q is not None and len(q) >= strategy_func.window:
            quotes_with_meta.append([q, sm.get(code, 0.5)])

    if not quotes_with_meta:
        return {}
    result = strategy_func(quotes_with_meta)
    return {code: float(score) for code, score in result}
