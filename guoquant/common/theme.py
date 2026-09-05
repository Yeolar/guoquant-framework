"""主题容器：一组成分股 Quote 的集合，theme 因子通过注册表惰性调用。

与 Quote（单只股票，见 common/quote.py）对应：Quote 经
``registered_quote_factors`` 注册单股因子，Theme 经
``registered_theme_factors`` 注册多股聚合因子（定义在
factors/theme_factors.py）。用法：

    t = Theme(quotes)                     # quotes 为一组 Quote
    t.tail_theme_limitup_count()          # 注册的 theme 因子（惰性缓存）
    len(t) / for q in t / t.quotes        # 容器语义

theme 因子函数签名：``factor(theme, *args, **kwargs)``，内部可直接
``for q in theme`` 遍历成分股。
"""

# 主题聚合因子注册表：函数名 → factor(theme, ...)
registered_theme_factors: dict = {}


def register_theme_factor(fn):
    """装饰器：把主题聚合因子按函数名注册（``theme.<函数名>()`` 调用）。

    Args:
        fn (callable): 主题聚合因子函数，签名 ``fn(theme, *args, **kwargs)``。

    Returns:
        callable: 传入的因子函数（装饰器原样返回）。
    """
    registered_theme_factors[fn.__name__] = fn
    return fn


class Theme(object):
    """一组成分股 Quote 的集合（可遍历、可 ``len``、因子惰性缓存）。

    与单股 Quote 的惰性因子机制对应：访问未定义属性时按名查
    ``registered_theme_factors``，命中则返回以 (因子名, 位置参数, 关键字参数)
    为缓存键的调用闭包（见 ``__getattr__``）；成分股保存在实例属性
    ``self.quotes``。

    Args:
        quotes (list | None): 成分股 Quote 序列；为 None 时初始化为空列表。
            默认 None。
    """

    def __init__(self, quotes=None):
        self.quotes = list(quotes) if quotes is not None else []
        self._factor_cache: dict = {}

    def __iter__(self):
        return iter(self.quotes)

    def __len__(self):
        return len(self.quotes)

    def __getattr__(self, name):
        """按名查 theme 因子注册表；命中则返回惰性缓存调用闭包。

        Args:
            name (str): 访问的属性名。

        Returns:
            callable: 以 (因子名, 位置参数, 关键字参数) 为缓存键的调用闭包，同一
                参数组合只计算一次。

        Raises:
            AttributeError: ``name`` 不在 ``registered_theme_factors`` 注册表中时。
        """
        if name not in registered_theme_factors:
            raise AttributeError(f"'Theme' object has no theme factor '{name}'")
        factor = registered_theme_factors[name]

        def factor_func(*args, **kwargs):
            key = (name, args, tuple(sorted(kwargs.items())))
            if key not in self._factor_cache:
                self._factor_cache[key] = factor(self, *args, **kwargs)
            return self._factor_cache[key]

        return factor_func
