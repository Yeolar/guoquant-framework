"""rank 命令：对股票/主题打分并输出排名 CSV（按 strategy/type 分流）。

用法示例：

.. code-block:: text

    python -m guoquant.cli rank --strategy stock_strength_strategy \\
        --root 250630/fetched_data
    python -m guoquant.cli rank --strategy stock_rise_rate_strategy
    python -m guoquant.cli rank --strategy theme_strength --type concept

策略解析约定（新增策略无需新建命令文件）：

- 股票策略：函数全名即策略名（如 ``stock_strength_strategy``），直接从
  ``guoquant.strategies`` 包取同名打分核心（``quotes`` 输入）；所需最少 K 线
  根数取该函数的 ``window`` 属性（``@window(n)`` 装饰器声明）。内置策略自动
  可用；用户策略在策略文件中定义同名打分核心并加 ``@window(n)`` 即可参与
  rank。
- 板块/无板块分支由 ``PLAIN_STRATEGIES`` 集合区分：集合内（当前
  ``stock_rise_rate_strategy``、``stock_user_momentum_strategy``）为无板块分支，
  不依赖 theme 排名与市场情绪文件；其余股票策略为板块分支，依赖
  ``{type}_theme_strength_score.csv`` 前置排名（市场情绪文件仅作适用阶段提示）。
- ``theme_strength``：独立分支（主题指数 + 板块成分，输出主题排名）。

输出文件（默认，均在 ``out/<日期>/rank/`` 下）：

- 板块分支：``{type}_{策略名}_score.csv`` （如
  ``industry_stock_strength_strategy_score.csv``），列 ``code, name, plate, score``；
- 无板块分支：``{策略名}_score.csv`` （不带 type 前缀），``plate`` 列留空；
- ``theme_strength``：``{type}_theme_strength_score.csv``，列 ``code, name, score``。

CSV 列序供下游 ``trace_stock`` / ``report`` 解析，改动需同步。
"""
import json
from datetime import date

from rich.progress import track
import typer

from guoquant.common.log import console
from guoquant.common.parallel import parallel
from guoquant.common.utils import outp, todate
from guoquant.common.data_path import *
from guoquant.common.phase import read_market_phase, STRATEGY_PHASES, PHASE_NAMES
from guoquant.common.quote import *
from guoquant.common.constants import STOCK_TOP_LIMIT, THEME_TOP_LIMIT
import guoquant.strategies as strategies
from guoquant.strategies import theme_strength_strategy

# 无板块分支策略（不依赖 theme 排名与市场情绪文件）；
# 新增此类用户策略时须把函数全名加入本集合（参照 stock_user_momentum_strategy）
PLAIN_STRATEGIES = {'stock_rise_rate_strategy', 'stock_user_momentum_strategy'}

# 板块分支中需要市值输入（三元组第 3 元）的策略——与引擎侧
# register_strategy 的 need_mv=True 配置一致；其余板块策略只收
# [Quote, 主题得分] 对。目前仅 stock_strength_strategy 在打分中使用市值。
MV_STRATEGIES = {'stock_strength_strategy'}


def _list_stock_strategies():
    """列出 ``strategies`` 包中可用的股票策略符号全名。

    Returns:
        list: 排序后的 ``stock_*_strategy`` 函数全名列表。
    """
    return sorted(n for n in dir(strategies)
                  if n.startswith('stock_') and n.endswith('_strategy'))


def _resolve_stock_strategy(name):
    """解析股票策略的打分核心并取所需最少 K 线根数。

    函数全名即策略名：从 ``strategies`` 包取同名函数作为打分核心，其
    ``window`` 属性由 ``@window(n)`` （``guoquant.common.strategy``）装饰器声明。
    策略不存在或未提供 ``window`` 属性时抛出 ``typer.BadParameter``。

    Args:
        name (str): 策略名（``strategies`` 包内的打分函数全名）。

    Returns:
        tuple: ``(打分函数, window)``，其中 ``window`` 为该策略所需最少 K 线
        根数。
    """
    fn = getattr(strategies, name, None)
    if fn is None or not hasattr(fn, 'window'):
        raise typer.BadParameter(
            f'策略 {name} 未提供 rank 打分函数（应定义 {name}(quotes)），'
            f'可用股票策略: {", ".join(_list_stock_strategies())}。'
            f'用户策略请在策略文件中定义同名打分函数，'
            f'并用 @window(n)（guoquant.common.strategy）声明最少K线根数。')
    return fn, fn.window


def _rank_plate(root, output, d, name, strategy_fn, window, type_):
    """板块分支：对成分股打分并输出板块股票排名 CSV。

    依赖 ``{type}_theme_strength_score.csv`` 主题排名与市场情绪文件。流程：读取
    情绪阶段 → 校验策略适用阶段（不匹配仅提示、仍运行）→ 加载主题排名与成分股
    → 对 ``MV_STRATEGIES`` 内需要市值的策略（当前仅 ``stock_strength_strategy``）
    叠加 filtered 市值 → 行情截断至 ``d`` 后打分。结果按 ``code, name, plate,
    score`` 写入 ``output``。

    数据格式约定：

    - ``{type}_theme_strength_score.csv``：``code, name, score`` （仅取前
      ``THEME_TOP_LIMIT`` 行参与选股）；
    - ``plate/{type}_plates/*.json``：含 ``plate.code`` / ``plate.name`` 与
      ``records[].code`` / ``records[].stock_name``；
    - filtered 股票 CSV：``code, name, 市值`` （第 3 列为 float 市值）。

    Args:
        root (Path): 数据根目录（含 ``plate/{type}_plates/`` 与行情子目录）。
        output (Path): 输出 CSV 路径（``{type}_{策略名}_score.csv``）。
        d (date): 打分截断日期，行情只保留 ``t <= d`` （杜绝未来函数）。
        name (str): 策略名（函数全名）。
        strategy_fn (callable): 打分核心函数，输入 ``[Quote, 主题得分]`` （需要
            市值者追加 ``[市值]``），输出 ``[(code, score), ...]``。
        window (int): 所需最少 K 线根数，历史不足的股票跳过。
        type_ (str): 板块类型 industry/concept（用于定位主题排名与板块文件）。
    """
    phase_path = output.with_name('market_sentiment_cycle.txt')
    # market_sentiment_cycle.txt 由 check_market_sentiment_cycle 命令生成，
    # 末行格式 E=0.52, M=0.012；read_market_phase 返回 (E, M, 阶段常量)
    e, m, phase = read_market_phase(phase_path)
    console.print(f'market phase: {PHASE_NAMES[phase]} (E={e:.2f}, M={m:+.3f})')
    allowed = STRATEGY_PHASES.get(name)
    if allowed is not None and phase not in allowed:
        # 策略不在当前情绪阶段适用范围内：仅黄色提示，仍照常运行（供观察）
        console.print(
            f'phase {PHASE_NAMES[phase]} not suitable for {name}, '
            'running for observation only', style='yellow bold')

    theme_scores = {}
    themes_meta = {}
    theme_ranks_path = output.with_name(f'{type_}_theme_strength_score.csv')
    with open(theme_ranks_path) as fp:
        # 只取排名前 THEME_TOP_LIMIT 的主题参与选股（主题聚焦）
        for line in fp.readlines()[:THEME_TOP_LIMIT]:
            parts = line.split(',')
            theme_scores[parts[0]] = float(parts[2].strip())
            themes_meta[parts[0]] = parts[1].strip()

    stock_theme_scores = {}
    stocks_meta = {}
    for path in (root / f'plate/{type_}_plates').iterdir():
        # 逐板块 JSON：只统计"主题在榜"板块的成分股，
        # 股票 → 所属主题得分（theme_scores）与主题名（themes_meta）
        if path.stem.split('_')[0] in theme_scores:
            with open(path) as fp:
                j = json.load(fp)
                code = j['plate']['code']
                for record in j['records']:
                    stock_theme_scores[record['code']] = theme_scores[code]
                    stocks_meta[record['code']] = [record['stock_name'], themes_meta[code]]

    stock_market_values = {}
    if name in MV_STRATEGIES:
        # filtered CSV 第 3 列为市值，仅对打分中实际使用市值的策略加载
        for path in filtered_stock_paths(root):
            with open(path) as fp:
                for line in fp.readlines():
                    items = line.split(',')
                    if items[0] in stock_theme_scores:
                        stock_market_values[items[0]] = float(items[2].strip())

    stock_paths = d_data_paths(root)
    stock_paths = [path for path in stock_paths
                   if path['key'] in stock_theme_scores]

    def _read(path):
        # 行情截断到 end_date=d：只保留 t<=d 的数据，杜绝未来函数
        return read_quote(path, end_date=d)

    quotes = []
    ignored = 0
    for q in parallel(_read, stock_paths, description='Loading...'):
        # 只保留历史长度 >= 策略窗口（window）的股票，样本不足的跳过
        if len(q) >= window:
            # 打分函数约定：输入 [Quote, 主题得分]（需要市值者追加 [市值]），
            # 输出 [(code, score), ...]；与各策略模块的解包形状一一对应
            entry = [q, stock_theme_scores[q.key]]
            if name in MV_STRATEGIES:
                entry.append(stock_market_values[q.key])
            quotes.append(entry)
        else:
            ignored += 1
    console.print(f'ignore n<{window} stock count={ignored}', style='yellow bold')

    result = strategy_fn(track(quotes, console=console))
    if len(result) == 0:
        console.print('rank result is empty', style='yellow bold')
        return

    with open(output, 'w') as fp:
        # CSV 列格式：code, name, plate, score —— 与 trace_stock 解析约定一致
        for i, p in enumerate(result):
            key, score = p
            name_, plate = stocks_meta[key]
            item = f'{key}, {name_}, {plate}, {score}'
            fp.write(item + '\n')
            if i < STOCK_TOP_LIMIT:
                console.print(item)


def _rank_plain(root, output, d, strategy_fn, window):
    """无板块分支：对股票打分并输出排名 CSV（不依赖 theme 排名与市场情绪）。

    用于 ``PLAIN_STRATEGIES`` 内策略（``stock_rise_rate_strategy`` /
    ``stock_user_momentum_strategy`` 等）。策略输入为 ``[Quote, 市值]`` 对，
    市值取自 filtered 白名单 CSV（此处无条件附带市值，与 ``need_mv=True``
    策略的引擎条目形状一致）。结果写入 ``output``，``plate`` 列留空，保持 4 列
    格式与板块分支一致。

    Args:
        root (Path): 数据根目录。
        output (Path): 输出 CSV 路径（``{策略名}_score.csv``，不带 type 前缀）。
        d (date): 打分截断日期，行情只保留 ``t <= d``。
        strategy_fn (callable): 打分核心函数，输入 ``[Quote, 市值]``，输出
            ``[(code, score), ...]``。
        window (int): 所需最少 K 线根数，历史不足的股票跳过。
    """
    stocks_meta = {}
    stock_market_values = {}
    for path in filtered_stock_paths(root):
        with open(path) as fp:
            for line in fp.readlines():
                items = line.split(',')
                stocks_meta[items[0]] = items[1].strip()
                stock_market_values[items[0]] = float(items[2].strip())

    stock_paths = d_data_paths(root)

    def _read(path):
        return read_quote(path, end_date=d)

    quotes = []
    ignored = 0
    for q in parallel(_read, stock_paths, description='Loading...'):
        if len(q) >= window:
            quotes.append([
                q,
                stock_market_values[q.key],
            ])
        else:
            ignored += 1
    console.print(f'ignore n<{window} stock count={ignored}', style='yellow bold')

    result = strategy_fn(track(quotes, console=console))
    if len(result) == 0:
        console.print('rank result is empty', style='yellow bold')
        return

    with open(output, 'w') as fp:
        for i, p in enumerate(result):
            key, score = p
            name_ = stocks_meta[key]
            # 无板块策略的 plate 列留空，保持 4 列格式与板块分支一致
            item = f'{key}, {name_}, , {score}'
            fp.write(item + '\n')
            if i < STOCK_TOP_LIMIT:
                console.print(item)


def _rank_theme(root, output, d, type_):
    """主题分支：对主题指数 + 板块成分打分，输出主题排名 CSV。

    依赖：``quote/d/index/`` 下以 ``SH.LIST`` 开头的主题指数行情文件（枚举见
    ``common.data_path.index_d_data_paths``）与 ``plate/{type}_plates/*.json``
    板块成分。主题指数与其成分股都齐备、且历史长度均达
    ``theme_strength_strategy.window`` 才参与打分。输出
    ``{type}_theme_strength_score.csv``，列为 ``code, name, score``，是板块分支
    rank 的 theme 得分输入（亦被 ``report`` / ``trace_stock`` 消费）。

    Args:
        root (Path): 数据根目录。
        output (Path): 输出 CSV 路径（``{type}_theme_strength_score.csv``）。
        d (date): 打分截断日期，行情只保留 ``t <= d``。
        type_ (str): 板块类型 industry/concept（用于定位板块成分文件）。
    """
    theme_paths = index_d_data_paths(root)
    stock_paths = d_data_paths(root)

    def _read(path):
        return read_quote(path, end_date=d)

    theme_quotes = {}
    ignored = 0
    for q in parallel(_read, theme_paths, description='Loading...'):
        # 主题指数同样要求历史长度 >= 策略窗口（theme_strength_strategy.window）
        if len(q) >= theme_strength_strategy.window:
            theme_quotes[q.key] = q
        else:
            ignored += 1
    console.print(f'ignore n<{theme_strength_strategy.window}'
                  f' theme count={ignored}', style='yellow bold')

    stock_quotes = {}
    ignored = 0
    for q in parallel(_read, stock_paths, description='Loading...'):
        if len(q) >= theme_strength_strategy.window:
            stock_quotes[q.key] = q
        else:
            ignored += 1
    console.print(f'ignore n<{theme_strength_strategy.window}'
                  f' stock count={ignored}', style='yellow bold')

    quotes = []
    themes_meta = {}
    for path in (root / f'plate/{type_}_plates').iterdir():
        with open(path) as fp:
            j = json.load(fp)
            code = j['plate']['code']
            name_ = j['plate']['name']
            # 聚合每个板块的成分股行情；主题指数与成分股都齐备才参与打分
            qs = [stock_quotes[record['code']] for record in j['records']
                  if record['code'] in stock_quotes]
            if code in theme_quotes and len(qs) > 0:
                quotes.append([theme_quotes[code], qs])
                themes_meta[code] = name_

    result = theme_strength_strategy(track(quotes, console=console))
    with open(output, 'w') as fp:
        for i, p in enumerate(result):
            key, score = p
            name_ = themes_meta[key]
            item = f'{key}, {name_}, {score}'
            fp.write(item + '\n')
            if i < THEME_TOP_LIMIT:
                console.print(item)


def command(
    strategy: str = typer.Option(
        'stock_strength_strategy',
        '--strategy',
        help='策略名：股票策略（默认 stock_strength_strategy；含 '
             'stock_rise_rate_strategy 等无板块策略）或 theme_strength（主题排名）',
    ),
    root: str = typer.Option(
        '{d:%y%m%d}/fetched_data',
        '--root',
        help='root directory, default={d:%%y%%m%%d}/fetched_data'),
    output: str = typer.Option(
        None,
        '-o', '--output',
        help='output csv file, default 见 command docstring（按分支命名）'),
    type: str = typer.Option(
        'industry',
        '--type',
        help='plate type: industry/concept, default=industry'),
    date: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--date',
        help=f'date, default={date.today():%y%m%d}'),
) -> None:
    """对股票/主题打分并输出排名 CSV。

    股票策略名 = ``strategies`` 包内打分核心的函数全名（``stock_<…>_strategy``）；
    ``theme_strength`` 为主题排名入口。按 ``strategy`` 分三个分支：

    - 板块分支（默认，不在 ``PLAIN_STRATEGIES`` 的股票策略）：依赖 theme 排名
      与市场情绪文件，输出 ``{type}_{策略名}_score.csv``，列
      ``code, name, plate, score``；
    - 无板块分支（``PLAIN_STRATEGIES``：``stock_rise_rate_strategy`` /
      ``stock_user_momentum_strategy``）：仅依赖 filtered 市值数据，输出
      ``{策略名}_score.csv`` （不带 type 前缀），``plate`` 列为空；
    - ``theme_strength``：主题指数 + 成分股打分，输出
      ``{type}_theme_strength_score.csv``，列 ``code, name, score``。

    默认输出位于 ``out/<日期>/rank/`` 下（可用 ``-o`` 覆盖）。输出列序供下游
    ``trace_stock`` / ``report`` 解析，改动需同步。

    Args:
        strategy (str): 策略名：股票策略（默认 ``stock_strength_strategy``，
            含 ``stock_rise_rate_strategy`` 等无板块策略）或
            ``theme_strength`` （主题排名）。
        root (str): 数据根目录，default=``{d:%y%m%d}/fetched_data``。
        output (str): 输出 CSV 文件；缺省按分支命名（见行为描述）。
        type (str): 板块类型 industry/concept，default=industry。
        date (str): 用于 ``root`` / ``output`` 模板的日期（YYMMDD），
            default=今天。
    """
    d = todate(date)
    type_ = type
    root = outp(root.format(d=d))
    if output is None:
        if strategy == 'theme_strength':
            output = outp(f'{d:%y%m%d}/rank/{type_}_theme_strength_score.csv')
        elif strategy in PLAIN_STRATEGIES:
            output = outp(f'{d:%y%m%d}/rank/{strategy}_score.csv')
        else:
            output = outp(f'{d:%y%m%d}/rank/{type_}_{strategy}_score.csv')
    else:
        output = outp(output.format(d=d, type=type_))

    if strategy == 'theme_strength':
        console.print(f'rank themes -> {output}')
        _rank_theme(root, output, d, type_)
        return

    strategy_fn, window = _resolve_stock_strategy(strategy)
    console.print(f'rank stocks -> {output}')
    if strategy in PLAIN_STRATEGIES:
        _rank_plain(root, output, d, strategy_fn, window)
    else:
        _rank_plate(root, output, d, strategy, strategy_fn, window, type_)
