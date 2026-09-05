"""
实时交易打分模块（实盘入口）。

经策略注册表（``guoquant.common.strategy`` 的 ``registered_strategies``）按
策略名取引擎打分入口——入口由 ``strategies/*.py`` 内 ``@register_strategy``
自动生成（内部调 ``guoquant.common.scoring`` 的通用打分执行），对指定日期
计算全市场得分；被 ``commands/auto_trade.py`` 引用。

仅支持非 flow 策略（QMT 无资金流/筹码分布数据）：

- 注册表条目为 ``(compute_fn, needs_flow)``：``needs_flow=True`` 的策略需要
  flow/dist 数据，实盘环境直接拒绝；
- 打分统一以 ``flow_dfs=None``、``dist_records=None`` 调用：非 flow 引擎入口
  不读取这两个参数，flow 策略又已被上一步拒绝，不会因缺数据而静默出错。
"""
from datetime import date

# 实盘入口：导入 guoquant.strategies 包即触发全部 @register_strategy
# 注册（包内自动扫描 strategies/*.py，见 strategies/__init__.py）
import guoquant.strategies  # noqa: F401
from guoquant.common.strategy import (
    registered_strategies,
    get_strategy_names as _get_strategy_names,
)

# QMT 不支持的策略（需要 flow + dist 数据；键即函数全名）
_UNSUPPORTED_STRATEGIES = {'stock_flow_strategy'}


def get_trade_strategy_names():
    """返回实盘可用策略名列表。

    在全部已注册策略名基础上排除 QMT 不支持的 flow 策略（见模块级
    ``_UNSUPPORTED_STRATEGIES``）。

    Returns:
        list: 实盘可用策略名列表。
    """
    return [s for s in _get_strategy_names()
            if s not in _UNSUPPORTED_STRATEGIES]


def compute_scores(quote_dfs, trade_date, strategy_name):
    """
    对指定日期计算全市场股票的得分。

    先校验策略存在且非 QMT 不支持的 flow 策略，再以 ``flow_dfs=None``、
    ``dist_records=None`` 调用注册表中的引擎打分入口（非 flow 入口不读取
    这两个参数）；打分只使用截至 ``trade_date`` 的数据，无前视偏差。

    Args:
        quote_dfs (dict): ``{code: DataFrame}`` 历史行情（datetime 索引，含
            o / h / l / c / v / tr / lc 等列；可直接传
            ``quote.load_quote_dfs`` 的整表加载结果）。
        trade_date (date): 打分截止日期（date 对象）。
        strategy_name (str): 策略名（须为注册表中已注册策略）。

    Returns:
        dict: ``{code: float}`` 得分 dict。

    Raises:
        ValueError: 策略不存在，或属于需要 flow/dist 数据的 QMT 不支持策略。
    """
    if strategy_name not in registered_strategies:
        raise ValueError(
            f'未知策略: {strategy_name}，可用: {get_trade_strategy_names()}')

    if strategy_name in _UNSUPPORTED_STRATEGIES:
        raise ValueError(
            f'策略 {strategy_name} 需要 flow/dist 数据，QMT 不支持')

    compute_fn, needs_flow = registered_strategies[strategy_name]
    if needs_flow:
        raise ValueError(
            f'策略 {strategy_name} 需要 flow/dist 数据，QMT 不支持')

    scores = compute_fn(quote_dfs, trade_date, flow_dfs=None, dist_records=None)
    return scores
