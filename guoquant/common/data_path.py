"""数据路径模块：按约定的目录结构枚举行情/资金流/筹码快照/股票池文件路径。

数据根目录 data_root（即 ``out/<日期>/fetched_data``）的目录约定：

- ``quote/{d|w}/stock/{code}.parquet``：K线；``d`` = 日线、``w`` = 周线，
  ``stock/`` 与 ``index/`` 平行
- ``quote/{d|w}/index/SH.LIST*.parquet``：行业/主题指数K线（本模块只枚举以
  ``SH.LIST`` 开头者；目录中可能另有 ``SH.000300`` 等大盘指数）
- ``flow/{d|w}/{code}.parquet``：资金流向，周期子目录内文件直接平铺
- ``dist/{code}.parquet``：筹码分布快照，不分周期、无子目录
- ``data_root/../stockfiltered/stock_mv_{c}.csv``：市值分层白名单股票池
  （与数据根同级）

说明：

- ``quote/`` 下还可存放 1/3/5 分钟线；``flow/`` 下还可有 ``i``、``m`` 等
  周期子目录（分时/月），``*_paths`` 只针对回测/排名使用的 d/w 枚举；
- ``MV_STEP = [10, 30, 100, 300]``，单位亿元，只用于 stockfiltered 白名单
  分层（``fetch_stockfiltered`` 命令产出，每档一个 CSV），行情/资金流/快照
  文件不再按市值分层存放。

被引用方：

- ``guoquant/commands/rank.py``：用 ``d_data_paths`` / ``index_d_data_paths``
  / ``filtered_stock_paths`` 枚举行情、主题指数与股票池文件；
- ``guoquant/commands/check_market_sentiment_cycle.py``：用 ``d_data_paths``
  枚举全市场个股行情；
- ``guoquant/common/trade/data.py``、``common/backtest/data.py``：向 runner
  与 ``auto_trade`` 等下游提供数据，经 ``filtered_stock_paths`` /
  ``load_industry_map`` 取股票池与行业映射；
- ``guoquant/common/fetch/plates.py``：用 ``latest_data_root`` 定位最新快照
  目录。
"""
from guoquant.common.utils import BASE_DIR
from pathlib import Path

# 流通市值分层下限（亿元）：相邻两项构成 [下限, 上限) 区间，最后一档无上限（≥300 亿）
MV_STEP = [10, 30, 100, 300]


def _index_quote_paths(quote_root, k):
    """枚举指数K线路径（``quote_root/{k}/index/`` 下以 ``SH.LIST`` 开头的文件）。

    Args:
        quote_root (Path): ``quote`` 数据目录。
        k (str): 周期子目录名（``'d'`` 日线 / ``'w'`` 周线）。

    Returns:
        dict[str, Path]: 文件名 stem（指数代码）→ 文件路径。
    """
    paths = {}
    index_dir = quote_root/k/'index'
    if index_dir.is_dir():
        for path in index_dir.iterdir():
            if path.name.startswith('SH.LIST'):
                paths[path.stem] = path
    return paths

def _stock_quote_paths(quote_root, k):
    """枚举个股K线路径（``quote_root/{k}/stock/``，key 为文件名 stem 即股票代码）。

    Args:
        quote_root (Path): ``quote`` 数据目录。
        k (str): 周期子目录名（``'d'`` 日线 / ``'w'`` 周线）。

    Returns:
        dict[str, Path]: 文件名 stem（股票代码）→ 文件路径。
    """
    paths = {}
    stock_dir = quote_root/k/'stock'
    if stock_dir.is_dir():
        for path in stock_dir.iterdir():
            paths[path.stem] = path
    return paths

def _flow_paths(flow_root, k):
    """枚举资金流向路径（``flow_root/{k}/`` 下文件，key 为文件名 stem）。

    Args:
        flow_root (Path): ``flow`` 数据目录。
        k (str): 周期子目录名（``'d'`` 日线 / ``'w'`` 周线）。

    Returns:
        dict[str, Path]: 文件名 stem → 文件路径。
    """
    paths = {}
    dir_ = flow_root/k
    if dir_.is_dir():
        for path in dir_.iterdir():
            paths[path.stem] = path
    return paths

def _dist_paths(dist_root):
    """枚举筹码快照路径（``dist_root/`` 下文件，无 d/w 周期维度）。

    Args:
        dist_root (Path): ``dist`` 数据目录。

    Returns:
        dict[str, Path]: 文件名 stem → 文件路径。
    """
    paths = {}
    if dist_root.is_dir():
        for path in dist_root.iterdir():
            paths[path.stem] = path
    return paths

def _data_paths(data_root, k):
    """组装个股三类数据路径，返回 ``[{key, quote, flow, dist}]``。

    key 集合取自K线枚举结果，flow / dist 再按 key 键取值——三类文件由
    ``fetch_stockdata`` 等按同一清单抓取、应齐全；若某股只有K线而缺 flow /
    dist 文件，此处会抛 KeyError。

    Args:
        data_root (Path): 数据根目录。
        k (str): 周期子目录名（``'d'`` 日线 / ``'w'`` 周线）。

    Returns:
        list[dict[str, Path]]: 每只个股一条 ``{key, quote, flow, dist}`` 字典。

    Raises:
        KeyError: 个股缺 flow / dist 文件时。
    """
    q_paths = _stock_quote_paths(data_root/'quote', k)
    f_paths = _flow_paths(data_root/'flow', k)
    d_paths = _dist_paths(data_root/'dist')
    return [
        {
            'key': k,
            'quote': q_paths[k],
            'flow':  f_paths[k],
            'dist':  d_paths[k],
        } for k in q_paths]


def d_data_paths(data_root):
    """日线三类数据路径（K线/资金流/筹码快照齐全的个股）。

    返回 ``[{key, quote, flow, dist}]``，供 ``read_quote`` 完整组装 Quote 的
    文件入口使用（rank / ``check_market_sentiment_cycle`` 等命令）。

    Args:
        data_root (Path): 数据根目录（``fetched_data``）。

    Returns:
        list[dict[str, Path]]: 每只个股一条 ``{key, quote, flow, dist}`` 字典。
    """
    return _data_paths(data_root, 'd')
def w_data_paths(data_root):
    """周线三类数据路径（K线/资金流/筹码快照齐全的个股）。

    Args:
        data_root (Path): 数据根目录（``fetched_data``）。

    Returns:
        list[dict[str, Path]]: 每只个股一条 ``{key, quote, flow, dist}`` 字典。
    """
    return _data_paths(data_root, 'w')


def quote_d_data_paths(data_root):
    """仅日线K线路径 ``[{key, quote}]``，不读资金流/筹码快照。

    Args:
        data_root (Path): 数据根目录（``fetched_data``）。

    Returns:
        list[dict[str, Path]]: 每只个股一条 ``{key, quote}`` 字典。
    """
    q_paths = _stock_quote_paths(data_root/'quote', 'd')
    return [
        {
            'key': k,
            'quote': q_paths[k],
        } for k in q_paths]
def quote_w_data_paths(data_root):
    """仅周线K线路径 ``[{key, quote}]``。

    Args:
        data_root (Path): 数据根目录（``fetched_data``）。

    Returns:
        list[dict[str, Path]]: 每只个股一条 ``{key, quote}`` 字典。
    """
    q_paths = _stock_quote_paths(data_root/'quote', 'w')
    return [
        {
            'key': k,
            'quote': q_paths[k],
        } for k in q_paths]


def index_d_data_paths(data_root):
    """日线指数K线路径 ``[{key, quote}]``，key 为指数代码。

    Args:
        data_root (Path): 数据根目录（``fetched_data``）。

    Returns:
        list[dict[str, Path]]: 每只指数一条 ``{key, quote}`` 字典。
    """
    q_paths = _index_quote_paths(data_root/'quote', 'd')
    return [
        {
            'key': k,
            'quote': q_paths[k],
        } for k in q_paths]
def index_w_data_paths(data_root):
    """周线指数K线路径 ``[{key, quote}]``。

    Args:
        data_root (Path): 数据根目录（``fetched_data``）。

    Returns:
        list[dict[str, Path]]: 每只指数一条 ``{key, quote}`` 字典。
    """
    q_paths = _index_quote_paths(data_root/'quote', 'w')
    return [
        {
            'key': k,
            'quote': q_paths[k],
        } for k in q_paths]


def latest_data_root():
    """定位最新快照的数据根，返回其 ``fetched_data`` 路径。

    取 ``out/`` 下目录名（YYMMDD，字典序即日期序）最大、且含 ``fetched_data``
    子目录的日期目录。供 fetch/plates.py（个股板块本地反查）等需要“最新数据”
    的场景使用。

    Returns:
        Path | None: 最新 ``fetched_data`` 目录；尚无任何数据快照时返回 None。
    """
    out = BASE_DIR / 'out'
    best = None
    if out.is_dir():
        for p in out.iterdir():
            fetched = p / 'fetched_data'
            if fetched.is_dir() and (best is None or p.name > best.name):
                best = fetched
    return best


def filtered_stock_paths(data_root):
    """各市值分层白名单股票池 CSV 路径（``stockfiltered`` 目录与数据根同级）。

    返回 ``[stock_mv_10.csv, ..., stock_mv_300.csv]`` 路径列表，与 ``MV_STEP``
    各档一一对应；CSV 由 ``fetch_stockfiltered`` 命令生成，无表头，每行
    ``code, name, 流通市值（元）``，按市值区间过滤并剔除 ST 股。

    Args:
        data_root (Path): 数据根目录（``fetched_data``）。

    Returns:
        list[Path]: 各市值分层股票池 CSV 路径（与 ``MV_STEP`` 档位同序）。
    """
    return [data_root / f'../stockfiltered/stock_mv_{c}.csv'
            for c in [str(v) for v in MV_STEP]]


def load_industry_map(data_root):
    """加载行业分类映射 ``code → industry_code``。

    读取 ``data_root/plate/industry_plates/*.json``，该文件由
    ``fetch_platestocks`` 的 industry 分支生成，结构为
    ``{'plate': {'code': ...}, 'records': [{code, ...}, ...]}``，把同一板块下
    所有成分股映射到该板块代码。

    Args:
        data_root (str | Path): 数据根目录（``fetched_data``）。

    Returns:
        dict[str, str]: 股票代码 → 行业板块代码；目录不存在返回空 dict，单个
            文件解析失败跳过（不影响整体）。
    """
    import json
    plate_dir = Path(data_root) / 'plate' / 'industry_plates'
    result = {}
    if not plate_dir.exists():
        return result
    for fpath in plate_dir.iterdir():
        if fpath.suffix != '.json':
            continue
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            industry_code = data['plate']['code']
            for rec in data.get('records', []):
                result[rec['code']] = industry_code
        except Exception:
            pass
    return result
