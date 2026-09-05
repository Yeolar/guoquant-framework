"""策略基类与策略注册表。

每个策略文件（``strategies/*.py``）定义一个继承 ``Strategy`` 的策略类，并在
模块底部导出配套的模块级打分函数：

- 策略类：负责“单只股票”的因子计算（构造时 ``run()`` 一次，结果存 self）
- 模块级打分核心 ``xxx_strategy(quotes)``：批量构造策略对象 → ``is_valid``
  过滤 → 横向 rank 打分 → 返回 ``(code, score)`` 降序序列（rank / check 等
  命令直接调用；引擎经其注册的引擎入口执行，见下）

打分函数的注册与选择走本模块的注册表（``register_strategy`` /
``registered_strategies`` / ``get_strategy_names``），策略类本身无需登记；
所需最少K线根数用 ``window(n)`` 装饰器声明、经 ``fn.window`` 读取（引擎
打分入口与 rank / check 命令均按此取窗口）。

注册表与打分执行解耦（与类定义同置本模块）：

- 供引擎回测/实盘使用的策略（内置 + 用户扩展）在各自 ``strategies/*.py``
  模块内把“quotes 级打分核心”（函数全名即策略名，如
  ``stock_strength_strategy``）用 ``@register_strategy`` 装饰器注册——装饰器
  按 ``needs_flow`` / ``need_mv`` / ``has_theme`` 引擎配置自动生成引擎打分
  入口（适配闭包，调 common/scoring 的通用打分执行）存入注册表；导入
  ``guoquant.strategies`` 包（自动扫描）即完成注册，无需先导入 scorer。
  命令专用的 ``theme_strength`` / ``market_sentiment_cycle`` 不注册，由对应
  命令按函数符号直接调用打分核心。
- 引擎侧（``common/backtest/scorer.py`` 的 ``precompute_scores``、
  ``common/trade/scorer.py`` 的 ``compute_scores``）从本模块读取注册表，
  按策略名取引擎入口与 ``needs_flow`` 标记。
"""


from guoquant.common.scoring import compute_flow_scores, compute_generic_scores


class Strategy(object):
    """策略基类：封装单只股票的因子计算接口。

    构造时把 ``d_quote`` 存为实例属性 ``self.d_quote``，并在 ``run_at_once``
    为真时立即执行一次 ``run()``。子类需实现：

    - ``run()``：计算所需因子，保存为 ``self.xxx``，构造时默认立即执行
    - ``take()``：返回本策略用于横向比较的因子值列表（顺序即打分列顺序）
    - ``untake()``：计算结束后释放资源（本框架实现多为空操作）

    可选成员：

    - ``is_valid``：过滤条件（property），不满足的股票不参与打分
    - ``key``：股票代码（委托给 ``d_quote.key``）

    Args:
        d_quote (Quote): 单只股票的行情容器（见 guoquant.common.quote）。
        run_at_once (bool): 是否构造时立即执行 ``run()``；默认 True。
    """

    def __init__(self, d_quote, run_at_once=True):
        self.d_quote = d_quote
        if run_at_once:
            self.run()

    @property
    def key(self):
        return self.d_quote.key

    def run(self):
        raise NotImplementedError()
    def take(self):
        raise NotImplementedError()
    def untake(self):
        raise NotImplementedError()


# ──────────────────────────────────────────────────────────────────────── #
# 策略注册表（与打分执行解耦，见模块 docstring）                             #
# ──────────────────────────────────────────────────────────────────────── #

# 策略名 → (compute_fn, needs_flow_data)
registered_strategies: dict = {}


def register_strategy(fn=None, *, needs_flow=False,
                      need_mv=False, has_theme=True):
    """注册回测策略（装饰器，挂在 quotes 级打分核心上）。

    用法（注册键 = 函数全名，如 ``stock_strength_strategy``）：

        @register_strategy(need_mv=True, has_theme=True)
        @window(120)
        def stock_strength_strategy(quotes, index_close=None): ...

    注册表条目为自动生成的引擎打分入口（适配闭包）：签名与 runner / trade 的
    调用约定一致（参数 ``quote_dfs``、``end_date`` 必填，``flow_dfs`` /
    ``dist_records`` / ``sector_momentum`` 可选），返回 ``{code: score}``。
    内部按需调用 common/scoring 的 ``compute_flow_scores``：``needs_flow=True``
    时缺 flow/dist 数据返回空 dict；或调用 ``compute_generic_scores``，由
    ``need_mv`` / ``has_theme`` 决定元数据组装；最少K线根数取打分核心的
    ``.window``，由 ``@window`` 装饰器声明。rank 不查注册表，按同一函数名
    （策略名 == 函数全名）符号约定直接调用同一个打分核心（返回 ``(code, score)``
    降序序列）。

    Args:
        fn (callable | None): 打分核心 ``xxx_strategy(quotes)``，函数全名即
            注册名/策略名（``--strategy``、``rank --strategy``、trade 名单均用
            此名）；为 None 时返回装饰器本身（支持带参用法）。默认 None。
        needs_flow (bool): 需要 flow + dist 数据（缺失时打分返回空 dict）。
            默认 False。
        need_mv (bool): 附带市值估算。默认 False。
        has_theme (bool): 附带行业轮动动量（无映射时默认 0.5）。默认 True。

    Returns:
        callable: 装饰器；直接使用时返回原打分核心（可继续叠加 ``@window(n)``
            等装饰器）。
    """
    def deco(f):
        registered_strategies[f.__name__] = (_make_engine_adapter(f), needs_flow)
        return f

    def _make_engine_adapter(f):
        if needs_flow:
            def _compute_engine(quote_dfs, end_date, flow_dfs=None,
                                dist_records=None, sector_momentum=None):
                """引擎打分入口（register_strategy 按 needs_flow 自动生成）。"""
                if not flow_dfs or not dist_records:
                    return {}
                return compute_flow_scores(
                    quote_dfs, flow_dfs, dist_records, end_date, f,
                    sector_momentum=sector_momentum)
        else:
            def _compute_engine(quote_dfs, end_date, flow_dfs=None,
                                dist_records=None, sector_momentum=None):
                """引擎打分入口（register_strategy 按 need_mv/has_theme 自动生成）。"""
                return compute_generic_scores(
                    quote_dfs, end_date, f, need_mv=need_mv,
                    has_theme=has_theme, sector_momentum=sector_momentum)
        return _compute_engine

    if fn is None:
        return deco
    return deco(fn)


def get_strategy_names():
    """返回全部已注册策略名列表（含用户新增策略）。

    Returns:
        list[str]: 已注册策略名列表。
    """
    return list(registered_strategies.keys())


def window(n):
    """声明打分入口所需的最少K线根数（数据不足则跳过）。

    用法：挂在策略模块的 ``xxx_strategy`` 打分函数上：

        @window(560)
        def market_sentiment_cycle_strategy(quotes): ...

    装饰后该函数带 ``.window`` 属性，引擎打分入口与 rank / check 命令均按此
    过滤数据不足的股票/主题。

    Args:
        n (int): 最少K线根数。

    Returns:
        callable: 装饰器（设置 ``fn.window = n`` 后返回原函数）。
    """
    def deco(fn):
        fn.window = n
        return fn
    return deco
