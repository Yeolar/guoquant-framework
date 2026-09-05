"""行情数据封装模块：Quote 对象。

Quote 是策略与因子的统一数据入口，单只股票一个实例，聚合三类数据：

- K线行情（``load_quote``）：日线/周线 OHLCVA + 换手率 + PE + 昨收
- 资金流向（``load_flow``）：超大/大/中/小单流入（futu 资金流接口，qmt
  未支持）
- 筹码分布快照（``load_dist``）：当日各档位资金进出（仅最新一日）

被引用方：

- ``guoquant/factors/*``：所有注册因子（``tail_*`` / ``rolling_*`` /
  ``dist_*``）都以 Quote 为第一参数
- ``guoquant/strategies/*``：策略通过 ``self.d_quote.xxx`` 访问行情与因子
- ``guoquant/common/backtest/*``：runner / IC 分析整表预载
  （``load_quote_dfs`` 等）后，按日 ``make_quote`` / ``slice_to_date`` 切片
  组装 Quote 打分
- ``guoquant/common/trade/*``：实盘历史加载与实时快照拼接
  （``load_quote_dfs`` / ``build_quote``）
- ``guoquant/commands/*``：从文件读取（``read_quote`` 系列，经统一的
  ``load_quote_df`` / ``make_quote`` 组装）

数据格式约定（由 ``guoquant/common/fetch/*`` 落盘）：

- K线 parquet 列：``t`` (datetime), ``o``, ``c``, ``h``, ``l``, ``v`` (股),
  ``a`` (成交额), ``tr`` (换手率%), ``pe``, ``lc`` (昨收)
- 资金流 parquet 列：``t``, ``in_flow``, ``super_in_flow``,
  ``big_in_flow``, ``mid_in_flow``, ``sml_in_flow``, ``main_in_flow``
  （``main_in`` = super + big）
- 筹码快照 parquet 列：``capital_in_super`` / ``capital_out_super`` 等 8 个
  档位字段
"""
import numpy as np
import pandas as pd
from datetime import date
from pathlib import Path


# ── 因子注册表（与 Quote 同文件，避免循环导入）─────────────────────── #
# 因子模块（factors/*.py）用 @register_quote_factor 注册；Quote.__getattr__
# 按名查找，命中后返回闭包实现惰性求值与结果缓存。
registered_quote_factors: dict = {}


def register_quote_factor(fn):
    """注册单股因子：以函数名（``__name__``）为键存入 ``registered_quote_factors``。

    用法：用 ``@register_quote_factor`` 修饰因子函数，如 ``tail_rsi(quote, window)``，
    之后即可通过 ``quote.tail_rsi(window)`` 调用。

    Args:
        fn (callable): 单股因子函数，签名 ``fn(quote, *args, **kwargs)``。

    Returns:
        callable: 传入的因子函数（装饰器原样返回，可继续叠加其他装饰器）。
    """
    registered_quote_factors[fn.__name__] = fn
    return fn


# 回测截止日之后最多保留的“未来”K线条数（供需要后续行情的场景使用）
LATEST_N = 30


def _calc_status_vectorized(df):
    """向量化计算每日涨跌停状态 ``status``，替代逐行 apply，提速约 500 倍。

    状态取值约定：

    - ``2``：涨停（收盘价触及涨停价）
    - ``-2``：跌停
    - ``1``：收涨（未涨停）
    - ``-1``：收跌（未跌停）
    - ``0``：平盘

    涨跌停价 = 昨收（``lc``）× 幅度系数：创业板（300/301）/科创板（688）幅度
    20%，对应 119.9 / 80.1；主板其余代码幅度 10%，对应 109.9 / 90.1。系数留
    0.1 余量：行情价格保留两位小数，避免四舍五入把恰好涨停/跌停的收盘价误判
    为普通涨跌。判断优先级：先判涨跌停（覆盖），再对剩余行按 ``o`` / ``c``
    关系判涨跌，开盘等于收盘时退化为与昨收比较。

    Args:
        df (pd.DataFrame): K线 DataFrame，需含列 ``code`` / ``o`` / ``c`` /
            ``lc``。

    Returns:
        pd.Series: 与 ``df`` 同索引的整型状态序列（2 / -2 / 1 / -1 / 0）。
    """
    code_prefix = df['code'].str[3:6].to_numpy()
    o  = df['o'].to_numpy()
    c  = df['c'].to_numpy()
    lc = df['lc'].to_numpy()

    # code 形如 'SH.600000'：取第 4~6 位（证券代码前三位）判断板块，
    # 创业板(300/301)与科创板(688) 涨跌停幅度 20%，主板 10%
    is_gem = np.isin(code_prefix, ['300', '301', '688'])
    up_mult = np.where(is_gem, 119.9, 109.9)
    dn_mult = np.where(is_gem, 80.1,  90.1)

    # 价格×100 取整，与 昨收×幅度 取整比较（同一精度，避免浮点误差）
    c_int    = (c  * 100).astype(int)
    lc_up_i  = (lc * up_mult).astype(int)
    lc_dn_i  = (lc * dn_mult).astype(int)

    status = np.zeros(len(df), dtype=int)

    # Priority 1: limit up / limit down
    status[c_int >= lc_up_i] = 2
    status[c_int <= lc_dn_i] = -2

    # Priority 2: remaining → o/c relationship
    neutral = (status == 0)
    status[neutral & (o < c)] = 1
    status[neutral & (o > c)] = -1
    # o == c cases: lc < c → 1, lc > c → -1, lc == c → stays 0
    flat_eq = neutral & (o == c)
    status[flat_eq & (lc < c)] = 1
    status[flat_eq & (lc > c)] = -1

    return pd.Series(status, index=df.index)


class Quote(object):
    """单只股票的行情容器（策略与因子的统一数据入口）。

    ``key`` 为股票代码（如 ``'SH.600000'``，与 parquet 文件名 stem 一致）。
    因子通过属性访问惰性求值：访问未定义属性时，若名字在
    ``registered_quote_factors`` 注册表中，则返回闭包函数，首次调用计算结果并
    缓存（见 ``__getattr__``），例如 ``quote.tail_rsi(14)``。行情/资金流/筹码
    数据分别由 ``load_quote`` / ``load_flow`` / ``load_dist`` 载入并派生下述
    公开属性。

    Attributes:
        key (str): 股票代码。
        data / future_data (pd.DataFrame | None): 已载入K线，及载入日之后的
            “未来”K线（最多 ``LATEST_N`` 条），由 ``load_quote`` 设置。
        dt (pd.Timestamp | None): 最后一根K线对应时间（Unix 秒转 datetime），
            由 ``load_quote`` 设置。
        close / high / low / volume / amount / status (pd.Series): K线派生行情
            序列，由 ``load_quote`` 设置；``status`` 为涨跌停状态。
        change_rate (pd.Series): 今收/昨收 - 1，由 ``load_quote`` 设置。
        turnover_rate (pd.Series): 换手率（百分数），由 ``load_quote`` 设置。
        pe (pd.Series | None): 市盈率；源数据无 ``pe`` 列时为 None，由
            ``load_quote`` 设置。
        flow_data (pd.DataFrame | None): 资金流数据，由 ``load_flow`` 设置。
        in_flow / super_in / big_in / mid_in / sml_in / main_in (pd.Series):
            资金流各口径序列（``main_in`` = super + big），由 ``load_flow``
            设置。
        super_net / big_net / mid_net / sml_net / main_net (float): 各档位当日
            净流入（买入 - 卖出；``main_net`` = super + big），由 ``load_dist``
            设置。
        total_volume / main_volume / super_volume (float): 各口径双边成交量
            （``main_volume`` 为主力双边总量 = 超大 + 大单），由 ``load_dist``
            设置。
    """

    def __init__(self, key):
        self.key = key
        self._factor_cache = {}

    def load_quote(self, df, future_df=None):
        """载入K线行情并派生常用行情属性。

        ``t`` 为 Unix 秒时间戳，载入后统一转为 pandas datetime 存于 ``self.dt``；
        ``change_rate`` = 今收/昨收 - 1；``turnover_rate`` 为百分数（fetch 时已
        ×100）。载入时自动计算 ``status`` 列并按列名派生各属性。

        Args:
            df (pd.DataFrame | None): K线 DataFrame，需含列 ``o`` / ``c`` / ``h`` /
                ``l`` / ``v`` / ``a`` / ``tr`` / ``lc`` / ``code`` / ``t``，
                ``pe`` 列可选。载入时自动计算 ``status`` 列（涨跌停状态，见
                ``_calc_status_vectorized``）。传 None 时仅设置 ``data`` /
                ``future_data`` 后返回。
            future_df (pd.DataFrame | None): 载入日之后的K线（最多 ``LATEST_N``
                条），仅用于需要后续行情的场景；回测打分中不得使用，否则产生前视
                偏差。默认 None。
        """
        if df is not None:
            df['status'] = _calc_status_vectorized(df)
        if future_df is not None:
            future_df['status'] = _calc_status_vectorized(future_df)

        self.data = df
        self.future_data = future_df
        if df is None:
            return

        self.dt = pd.to_datetime(df.t.iloc[-1], unit='s')
        self.close = df.c
        self.high = df.h
        self.low = df.l
        self.volume = df.v
        self.amount = df.a
        self.status = df.status
        self.change_rate = df.c / df.lc - 1
        self.turnover_rate = df.tr
        self.pe = df.pe if 'pe' in df.columns else None

    def load_flow(self, df):
        """载入资金流向数据（可选）。

        保存 ``self.flow_data`` 并按列名 ``in_flow`` 等派生资金流属性。

        Args:
            df (pd.DataFrame | None): 资金流 DataFrame，需含列 ``in_flow`` /
                ``super_in_flow`` / ``big_in_flow`` / ``mid_in_flow`` /
                ``sml_in_flow`` / ``main_in_flow``，fetch 落盘时即为主力 = 超大 +
                大单口径。传 None 时不执行任何操作。
        """
        self.flow_data = df
        if df is None:
            return

        self.in_flow = df.in_flow
        self.super_in = df.super_in_flow
        self.big_in = df.big_in_flow
        self.mid_in = df.mid_in_flow
        self.sml_in = df.sml_in_flow
        self.main_in = df.main_in_flow   # super + big

    def load_dist(self, record):
        """载入筹码分布快照（可选，仅最新一日）。

        由 record 八个档位字段派生净流入与双边成交量。注意：快照只有“最新”一份，
        历史回测中用它等于用未来数据，存在前视偏差——仅实盘当日快照或对偏差不
        敏感的分析可用。

        Args:
            record (pd.Series | None): dist 快照最新记录，字段为
                ``capital_in_super`` / ``capital_out_super`` / ... /
                ``capital_out_small`` 八个档位（超大/大/中/小 × 买入/卖出）。
                传 None 时不执行任何操作。
        """
        if record is None:
            return

        super_in   = record.capital_in_super
        super_out  = record.capital_out_super
        big_in     = record.capital_in_big
        big_out    = record.capital_out_big
        mid_in     = record.capital_in_mid
        mid_out    = record.capital_out_mid
        sml_in     = record.capital_in_small
        sml_out    = record.capital_out_small

        # 各档位净流入 = 买入 - 卖出
        self.super_net = super_in  - super_out
        self.big_net   = big_in    - big_out
        self.mid_net   = mid_in    - mid_out
        self.sml_net   = sml_in    - sml_out
        self.main_net  = self.super_net + self.big_net

        self.total_volume = super_in + super_out + big_in + big_out + mid_in + mid_out + sml_in + sml_out
        self.main_volume  = super_in + super_out + big_in + big_out   # 主力双边总量
        self.super_volume = super_in + super_out

    def __len__(self):
        return len(self.data) if self.data is not None else 0

    def __getattr__(self, name):
        """因子惰性访问：属性名命中注册表时返回因子闭包并缓存结果。

        访问未定义属性时按名查 ``registered_quote_factors``，命中则返回以
        (因子名, 位置参数, 关键字参数) 为缓存键的调用闭包，同一参数组合只计算
        一次。

        Args:
            name (str): 访问的属性名。

        Returns:
            callable: 因子调用闭包 ``factor_func(*args, **kwargs)``。

        Raises:
            AttributeError: ``name`` 不在 ``registered_quote_factors`` 注册表中时。
        """
        if name not in registered_quote_factors:
            raise AttributeError(
                    f"'{type(self).__name__}' object has no factor '{name}'")

        factor = registered_quote_factors[name]

        def factor_func(*args, **kwargs):
            # 以 (因子名, 位置参数, 关键字参数) 为缓存键：
            # 同一参数组合只计算一次，供多个策略/回测日期复用
            key = (name, args, tuple(sorted(kwargs.items())))
            if key not in self._factor_cache:
                self._factor_cache[key] = factor(self, *args, **kwargs)
            return self._factor_cache[key]

        return factor_func

    @property
    def ma5(self):
        return self.rolling_price(5)
    @property
    def ma10(self):
        return self.rolling_price(10)
    @property
    def ma20(self):
        return self.rolling_price(20)
    @property
    def ma60(self):
        return self.rolling_price(60)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 文件读取与 Quote 组装（统一实现）                                          #
#                                                                         #
# 同一套底层同时服务两种入口：                                              #
#   - 文件入口（rank/check）：data_path.py 产 {key, quote, flow?, dist?}    #
#     路径 → load_quote_df 系列读文件，read_quote 组装 Quote；              #
#   - 内存入口（回测 / IC 分析 / 实盘引擎）：本文件 load_quote_dfs 整表预载 #
#     后经 make_quote / slice_to_date 逐日切片组装 Quote。                  #
# 统一格式：parquet / json 自动识别，返回以 datetime 为索引（保留 t 列）    #
# 的 DataFrame（json 旧格式 t 为 Unix 秒，按 unit='s' 转换）。              #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

def load_quote_df(path):
    """加载单只股票行情文件（parquet/json 自动识别）。

    返回以 datetime 为索引（保留 ``t`` 列）的升序 DataFrame；``t`` 列
    datetime64 直接使用，整数秒（旧 json 格式）按 ``unit='s'`` 转换。

    Args:
        path (str | Path): 行情文件路径（.parquet / .json）。

    Returns:
        pd.DataFrame: datetime 索引的行情数据。
    """
    df = pd.read_parquet(path) if str(path).endswith('.parquet') else _load_json_df(path)
    if not pd.api.types.is_datetime64_any_dtype(df['t']):
        df['t'] = pd.to_datetime(df['t'], unit='s')
    df['datetime'] = pd.to_datetime(df['t'])
    return df.set_index('datetime').sort_index()


def load_flow_df(path):
    """加载单只股票资金流文件。

    与行情读取同构（见模块 docstring 列约定），返回 datetime 索引升序
    DataFrame。

    Args:
        path (str | Path): 资金流文件路径（.parquet / .json）。

    Returns:
        pd.DataFrame: datetime 索引的资金流数据。
    """
    return load_quote_df(path)


def load_dist_record(path):
    """加载筹码分布快照文件，返回最后一行（最新快照）。

    parquet 直读取末行；json 旧格式为记录列表，取最后一项。

    Args:
        path (str | Path): 筹码快照文件路径（.parquet / .json）。

    Returns:
        pd.Series | dict: 最新快照记录（parquet 末行 / json 列表末项）。
    """
    try:
        return pd.read_parquet(path).iloc[-1]
    except Exception:
        import orjson
        j = orjson.loads(Path(path).read_bytes())
        return j[-1]


def _load_json_df(path):
    """旧 json 格式读取：行 dict 列表（``t`` 为 Unix 秒）转 DataFrame。

    Args:
        path (str | Path): json 文件路径。

    Returns:
        pd.DataFrame: 由行 dict 列表构造的 DataFrame。
    """
    import orjson
    return pd.DataFrame(orjson.loads(Path(path).read_bytes()))


# ── 目录级整表加载（回测 / 实盘 / IC 共用；单文件读取见上）────────────── #

def _load_dfs_dir(root, loader, min_rows):
    """遍历目录下全部 parquet/json 文件，按文件名 stem 为键加载。

    根目录不存在、单文件加载失败或行数不足 ``min_rows`` 时跳过该文件（单个
    损坏文件不影响整体加载）。

    Args:
        root (str | Path): 数据目录。
        loader (callable): 单文件加载函数，签名 ``loader(path)``。
        min_rows (int): 最少行数下限，不足则丢弃。

    Returns:
        dict[str, object]: 文件名 stem → 单文件加载结果。
    """
    result = {}
    if not Path(root).is_dir():
        return result
    for path in Path(root).iterdir():
        if path.suffix not in ('.json', '.parquet'):
            continue
        try:
            df = loader(path)
            if df is not None and len(df) >= min_rows:
                result[path.stem] = df
        except Exception:
            pass  # 单个文件损坏不影响整体加载
    return result


def load_quote_dfs(data_root, min_rows=20):
    """整表加载 ``fetched_data/quote/d/stock/`` 下全部K线 → ``{code: DataFrame}``。

    回测 runner、``ic_analysis`` 命令、实盘 ``auto_trade`` 的统一数据入口，均
    直接调用本函数（或 ``load_flow_dfs`` / ``load_dist_records``）。

    Args:
        data_root (str | Path): 数据根目录（``fetched_data``）。
        min_rows (int): 最少行数，不足的个股丢弃；默认 20。

    Returns:
        dict[str, pd.DataFrame]: 股票代码 → K线 DataFrame。
    """
    return _load_dfs_dir(Path(data_root) / 'quote' / 'd' / 'stock',
                         load_quote_df, min_rows)


def load_flow_dfs(data_root, min_rows=20):
    """整表加载 ``fetched_data/flow/d/`` 下全部资金流 → ``{code: DataFrame}``。

    该周期目录下文件直接平铺、无子目录。

    Args:
        data_root (str | Path): 数据根目录（``fetched_data``）。
        min_rows (int): 最少行数，不足的丢弃；默认 20。

    Returns:
        dict[str, pd.DataFrame]: 股票代码 → 资金流 DataFrame。
    """
    return _load_dfs_dir(Path(data_root) / 'flow' / 'd', load_flow_df, min_rows)


def load_dist_records(data_root):
    """整表加载 ``fetched_data/dist/`` 下全部筹码快照 → ``{code: 最新记录}``。

    注意：dist 为最新快照，历史回测中使用存在前视偏差（见 backtest/data.py
    说明）。

    Args:
        data_root (str | Path): 数据根目录（``fetched_data``）。

    Returns:
        dict[str, pd.Series | dict]: 股票代码 → 最新筹码快照记录。
    """
    result = {}
    root = Path(data_root) / 'dist'
    if not root.is_dir():
        return result
    for path in root.iterdir():
        if path.suffix not in ('.json', '.parquet'):
            continue
        try:
            rec = load_dist_record(path)
            if rec is not None:
                result[path.stem] = rec
        except Exception:
            pass
    return result


def slice_to_date(df, end_date):
    """截取 DataFrame 到 ``end_date`` 当日（含）为止并返回副本。

    用 ``iloc`` 定位 + ``get_indexer(ffill)`` 找到 ``end_date`` 当日（或之前
    最近）的行位置，避免对每行调用 ``.date`` 物化的开销——回测中会被高频调用。
    ``end_date`` 为 date 对象，比较时由 ``pd.Timestamp`` 自动补齐时间。

    Args:
        df (pd.DataFrame): datetime 索引的 DataFrame。
        end_date (date): 截止日期（含）。

    Returns:
        pd.DataFrame | None: 截至 ``end_date`` 当日（含）的行副本；``end_date`` 早于
            数据起点时返回 None。
    """
    ts = pd.Timestamp(end_date)
    pos = df.index.get_indexer([ts], method='ffill')[0]
    if pos < 0:
        return None
    return df.iloc[:pos + 1]


def make_quote(code, df, end_date=None, flow_df=None, dist_record=None,
               want_future=False):
    """统一 df → Quote 工厂：组装K线并按 ``end_date`` 截断，可选附带资金流与筹码
    快照。

    两个入口共用：引擎侧整表预载后逐日切片组装（``want_future=False``，回测
    打分不得使用未来数据）；文件读取侧（``read_quote`` 系列经 ``load_quote_df``
    取得 df 后调用，``want_future=True`` 时额外保留 ``end_date`` 之后最多
    ``LATEST_N`` 条K线至 ``future_data``，供需要后续行情的场景使用）。
    ``flow_df`` 同样按 ``end_date`` 截断；``dist_record`` 为最新快照不截断
    （历史回测中使用存在前视偏差，见 backtest/data.py 说明）。

    Args:
        code (str): 股票代码（写入 ``Quote.key``）。
        df (pd.DataFrame | None): K线数据；为空时返回 None。
        end_date (date | None): 截止日期（含）；None 表示不截断。默认 None。
        flow_df (pd.DataFrame | None): 资金流数据，按 ``end_date`` 截断后载入。
            默认 None。
        dist_record (pd.Series | dict | None): 筹码分布快照记录，原样载入、不
            截断。默认 None。
        want_future (bool): 是否保留 ``end_date`` 之后的“未来”K线（最多
            ``LATEST_N`` 条）至 ``future_data``。默认 False。

    Returns:
        Quote | None: 组装好的 Quote；``df`` 为空或 ``end_date`` 早于数据起点
            时返回 None。
    """
    if df is None or len(df) == 0:
        return None
    if end_date is None:
        sub_df, future_df = df, None
    else:
        ts = pd.Timestamp(end_date)
        pos = df.index.get_indexer([ts], method='ffill')[0]
        if pos < 0:
            return None
        sub_df = df.iloc[:pos + 1]
        future_df = None
        if want_future and pos + 1 < len(df):
            future_df = df.iloc[pos + 1:pos + 1 + LATEST_N]

    quote = Quote(code)
    quote.load_quote(_reset(sub_df), _reset(future_df))
    if flow_df is not None:
        sub_flow = flow_df if end_date is None else slice_to_date(flow_df, end_date)
        if sub_flow is not None and len(sub_flow) > 0:
            quote.load_flow(_reset(sub_flow))
    if dist_record is not None:
        quote.load_dist(dist_record)
    return quote


def _reset(df):
    """datetime 索引 → 普通行索引（``load_quote`` / ``load_flow`` 按列名访问）。

    Args:
        df (pd.DataFrame | None): 待处理 DataFrame。

    Returns:
        pd.DataFrame | None: 重置索引后的 DataFrame；输入为 None 或空时返回
            None。
    """
    return df.reset_index() if df is not None and len(df) > 0 else None


def read_quote_by_quote_path(path, end_date):
    """只读K线：path 为单只股票的文件路径，key 取文件名 stem。

    Args:
        path (Path): 行情文件路径。
        end_date (date): 截止日期（含）。

    Returns:
        Quote | None: 组装好的 Quote；``end_date`` 早于数据起点时返回 None。
    """
    return make_quote(path.stem, load_quote_df(path), end_date, want_future=True)


def read_quote(path, end_date):
    """完整读取：path 为 ``{key, quote, flow?, dist?}`` 字典（见 data_path.py）。

    依次载入K线、资金流、筹码快照；``flow`` / ``dist`` 键缺失时跳过对应数据，
    后续访问相关因子（如 ``tail_main_flow_rate``）会因缺少属性而报错。

    Args:
        path (dict): 路径字典；``key`` 缺失时取 ``quote`` 文件名的 stem。
        end_date (date): 截止日期（含）。

    Returns:
        Quote | None: 组装好的 Quote；``end_date`` 早于数据起点时返回 None。
    """
    return make_quote(
        path.get('key', Path(path['quote']).stem),
        load_quote_df(path['quote']),
        end_date,
        flow_df=(load_flow_df(path['flow']) if 'flow' in path else None),
        dist_record=(load_dist_record(path['dist']) if 'dist' in path else None),
        want_future=True)
