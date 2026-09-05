"""自动交易管理命令

基于 QMT xttrader 实盘执行策略调仓。
依赖：``guoquant.common.trade.*`` （executor 下单、scorer 打分、strategy 调仓
计划）。
代码格式：内部统一使用 Futu 格式 ``SZ.000001``，与 QMT 交互时在边界处转换。

用法示例：

.. code-block:: text

    # 模拟运行（仅计算不执行）
    python -m guoquant.cli auto_trade --strategy stock_strength_strategy \\
        --root 260719/fetched_data --dry-run

    # 实盘执行
    python -m guoquant.cli auto_trade --strategy stock_strength_strategy \\
        --root 260719/fetched_data --mini-path /path/to/QMT/userdata_mini \\
        --account 12345678 --top-n 10
"""
import json
import re
import textwrap
from datetime import date, datetime
from pathlib import Path

import typer

from guoquant.common.log import console
from guoquant.common.utils import outp, todate

from guoquant.common.fetch.qmtapi.context import to_qmt_code


# QMT 格式 → Futu 格式转换（'000001.SZ' → 'SZ.000001'）
_QMT_CODE_RE = re.compile(r'^(\d{6})\.(SZ|SH)$')


def _qmt_to_futu(qmt_code):
    """把 QMT 返回的 ``000001.SZ`` 代码转成内部统一使用的 Futu 格式 ``SZ.000001``。

    无法匹配（非 A 股格式）时原样返回，避免破坏基金/港股等其它代码。

    Args:
        qmt_code (str): QMT 格式代码（6 位数字 + 市场后缀）。

    Returns:
        str: Futu 格式代码；非 A 股格式（无法匹配）时原样返回。
    """
    m = _QMT_CODE_RE.match(qmt_code)
    if m:
        return f'{m.group(2)}.{m.group(1)}'
    return qmt_code


def command(
    strategy: str = typer.Option(
        'stock_strength_strategy',
        '--strategy',
        help='交易策略（打分核心函数全名），default=stock_strength_strategy。'
             '不支持 stock_flow_strategy（QMT 无资金流数据）',
    ),
    root: str = typer.Option(
        '{d:%y%m%d}/fetched_data',
        '--root',
        help='数据根目录，default={d:%%y%%m%%d}/fetched_data',
    ),
    mini_path: str = typer.Option(
        None,
        '--mini-path',
        help='QMT 迷你端 userdata_mini 路径（实盘必填）',
    ),
    account: str = typer.Option(
        None,
        '--account',
        help='资金账号（实盘必填）',
    ),
    top_n: int = typer.Option(
        10,
        '--top-n',
        help='持仓只数上限，default=10',
    ),
    capital: float = typer.Option(
        None,
        '--capital',
        help='计划投入资金，default=当前账户可用资金',
    ),
    dry_run: bool = typer.Option(
        False,
        '--dry-run',
        help='仅计算选股和调仓计划，不实际执行下单',
    ),
    select_file: str = typer.Option(
        None,
        '--select-file',
        help='人工选股文件路径（每行一个代码，或 JSON 数组）',
    ),
    select_codes: str = typer.Option(
        None,
        '--select-codes',
        help='人工选股代码列表，逗号分隔，如 SZ.000001,SH.600000',
    ),
    positions_file: str = typer.Option(
        None,
        '--positions-file',
        help='持仓状态文件路径，用于保存/加载止损追踪状态',
    ),
    candidates_only: bool = typer.Option(
        False,
        '--candidates-only',
        help='仅输出候选股列表（含得分和行业），不计算调仓计划',
    ),
    output: str = typer.Option(
        '{d:%y%m%d}/auto_trade',
        '--output',
        help='输出目录，default={d:%%y%%m%%d}/auto_trade',
    ),
    date: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--date',
        help=f'用于 root/output 模板的日期，default={date.today():%y%m%d}',
    ),
) -> None:
    """基于 QMT xttrader 执行策略自动交易。

    支持两种模式：

    - 全自动：策略打分 → 行业分散选股 → 执行调仓；
    - 人工选股：使用 ``--select-file`` （每行一个代码或 JSON 数组）或
      ``--select-codes`` （逗号分隔）指定买入名单，跳过策略选股（仍计算分数供
      排序参考）。

    实盘模式需要 ``--mini-path`` 与 ``--account``；``--dry-run`` 仅计算选股与
    调仓计划，不实际下单。交易策略限 ``VALID_STRATEGIES`` 白名单（不含
    ``stock_flow_strategy``，QMT 无资金流数据源）。QMT 需先启动并登录，确保
    xttrader 可连接。

    行为分支说明：

    - ``--candidates-only``：仅输出候选股列表（``candidates.json`` 与
      ``candidates.txt``，含得分与行业）后退出；
    - 常规模式：终端打印调仓计划（止损触发提示、卖出/买入/继续持有明细），
      实盘执行买卖单并更新止损追踪，把止损状态保存到 ``--positions-file``
      （缺省 ``<output>/stop_loss_state.json``），再把本次调仓决策完整记录到
      ``<output>/trade_plan.json`` （含目标/买卖/止损/持仓，供复盘与审计）。

    Args:
        strategy (str): 交易策略（打分核心函数全名），
            default=``stock_strength_strategy``。
        root (str): 数据根目录，default=``{d:%y%m%d}/fetched_data``。
        mini_path (str): QMT 迷你端 ``userdata_mini`` 路径（实盘必填）。
        account (str): 资金账号（实盘必填）。
        top_n (int): 持仓只数上限，default=10。
        capital (float): 计划投入资金，default=当前账户可用资金。
        dry_run (bool): 仅计算选股和调仓计划，不实际执行下单。
        select_file (str): 人工选股文件路径（每行一个代码，或 JSON 数组）。
        select_codes (str): 人工选股代码列表，逗号分隔，如
            ``SZ.000001,SH.600000``。
        positions_file (str): 持仓状态文件路径，用于保存/加载止损追踪状态。
        candidates_only (bool): 仅输出候选股列表（含得分和行业），不计算调仓
            计划。
        output (str): 输出目录，default=``{d:%y%m%d}/auto_trade``。
        date (str): 用于 ``root`` / ``output`` 模板的日期（YYMMDD），
            default=今天。
    """
    VALID_STRATEGIES = (
        'stock_strength_strategy', 'stock_main_wave_strategy',
        'stock_lowend_startup_strategy', 'stock_pullback_strategy',
        'stock_ma_cluster_breakout_strategy', 'stock_rise_rate_strategy',
    )
    # 实盘策略白名单与回测注册表不同：此处硬编码且不含 flow
    # （QMT 无资金流数据源），新增实盘策略需同步维护
    if strategy not in VALID_STRATEGIES:
        console.print(
            f'Error: 未知策略 {strategy}（可选: {", ".join(VALID_STRATEGIES)}）',
            style='red bold',
        )
        raise typer.Exit(1)

    d = todate(date)
    data_root = outp(root.format(d=d))
    output_dir = outp(output.format(d=d), is_dir=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    strategy_name = strategy
    account_id = account

    # 实盘模式强制要求 QMT 路径与账号；dry-run 只计算不下单，可缺省
    if not dry_run and (not mini_path or not account_id):
        console.print(
            'Error: 实盘模式需要 --mini-path 和 --account 参数，'
            '或使用 --dry-run 仅计算不执行',
            style='red bold',
        )
        raise typer.Exit(1)

    select_codes_str = select_codes
    manual_select = bool(select_file or select_codes_str)

    mode_label = '人工选股' if manual_select else '策略选股'
    console.print(f'[自动交易] 模式={mode_label}  策略={strategy_name}  数据={data_root}')
    console.print(f'           持仓上限={top_n}只  mode='
                   f'{"模拟" if dry_run else "实盘"}')
    console.print('')

    # ── 1. 连接 QMT ─────────────────────────────────────────────────
    executor = None
    if not dry_run:
        from guoquant.common.trade.executor import QmtExecutor
        executor = QmtExecutor(mini_path, account_id)
        if not executor.connect():
            console.print('Error: 连接 QMT 交易接口失败，请检查 QMT 是否已启动', style='red bold')
            raise typer.Exit(1)

        asset_info = executor.query_asset()
        positions_qmt = executor.query_positions()

        console.print(f'  账户连接成功')
        if asset_info:
            console.print(f'  总资产: {asset_info["total_asset"]:>12,.2f}  '
                           f'持仓市值: {asset_info["market_value"]:>12,.2f}  '
                           f'可用资金: {asset_info["cash"]:>12,.2f}')

        # 转换持仓 key 为 Futu 格式：QMT 返回 '000001.SZ'，内部统一 'SZ.000001'
        raw_positions = {}
        for qmt_code, pos in positions_qmt.items():
            futu_code = _qmt_to_futu(qmt_code)
            raw_positions[futu_code] = {
                'volume': pos['volume'],
                'avg_price': pos['open_price'],
            }

        # 投入资金：未指定 --capital 时用账户总资产兜底
        total_cash = capital or (asset_info['total_asset']
                                 if asset_info else 1_000_000)
    else:
        # dry-run：无真实持仓，模拟空仓与默认资金
        raw_positions = {}
        total_cash = capital or 1_000_000

    # ── 2. 加载数据 ─────────────────────────────────────────────────
    console.print('  加载行情数据...')
    from guoquant.common.quote import load_quote_dfs
    from guoquant.common.trade.data import (
        load_filtered_stocks,
        load_industry_map,
        fetch_latest_quote,
        build_all_quotes,
    )
    quote_dfs = load_quote_dfs(data_root)
    if not quote_dfs:
        console.print(f'Error: 未找到行情数据: {data_root}/quote/d/', style='red bold')
        raise typer.Exit(1)
    console.print(f'  已加载 {len(quote_dfs)} 只股票历史行情')

    # 加载筛选股票列表（filtered 市值分层白名单；缺失不影响主流程，
    # 当前仅打印数量，未参与选股限制）
    filtered_codes = None
    try:
        filtered_codes = load_filtered_stocks(data_root)
        if filtered_codes:
            console.print(f'  筛选股票: {len(filtered_codes)} 只')
    except Exception:
        pass

    # 加载行业分类
    industry_map = load_industry_map(data_root)
    console.print(f'  行业映射: {len(industry_map)} 只')

    # ── 3. 打分 ─────────────────────────────────────────────────────
    today = datetime.today().date()
    scores = {}

    if manual_select:
        # 人工选股模式：仍计算分数（用于排序参考），但不过滤
        console.print(f'  计算 {today} 选股分数（策略={strategy_name}）...')
        from guoquant.common.trade.scorer import compute_scores
        scores = compute_scores(quote_dfs, today, strategy_name)
        console.print(f'  有得分股票: {len(scores)} 只（仅供参考）')

        # 读取人工选股：优先用 --select-codes 逗号列表，其次读 --select-file 文件
        from guoquant.common.trade.strategy import load_selected_codes
        if select_codes_str:
            target_codes = [c.strip() for c in select_codes_str.split(',') if c.strip()]
        else:
            target_codes = load_selected_codes(select_file, scores=scores)
        console.print(f'  人工选股: {len(target_codes)} 只')
    else:
        console.print(f'  计算 {today} 选股分数（策略={strategy_name}）...')
        from guoquant.common.trade.scorer import compute_scores
        scores = compute_scores(quote_dfs, today, strategy_name)
        console.print(f'  有得分股票: {len(scores)} 只')

        # ── 4. 选股 ─────────────────────────────────────────────────
        from guoquant.common.trade.strategy import select_top_stocks
        # 按分数取前 top_n，并用行业映射做分散（避免同行业扎堆）
        target_codes = select_top_stocks(scores, top_n, industry_map)

    console.print(f'  目标持仓: {len(target_codes)} 只')
    for i, code in enumerate(target_codes):
        sc = scores.get(code, 0)
        ind = industry_map.get(code, '---')
        console.print(f'    {i+1:>2}. {code}  得分={sc:.4f}  行业={ind}')

    # ── candidates-only 模式：仅输出候选，保存后退出 ────────────────
    if candidates_only:
        candidates_path = output_dir / 'candidates.json'
        candidates = [
            {'rank': i + 1, 'code': c, 'score': scores.get(c, 0),
             'industry': industry_map.get(c, '')}
            for i, c in enumerate(target_codes)
        ]
        with open(candidates_path, 'w', encoding='utf-8') as f:
            json.dump(candidates, f, ensure_ascii=False, indent=2)
        console.print(f'  候选股已保存: {candidates_path}', style='green')

        # 也输出纯文本格式，方便直接作为选股文件
        txt_path = output_dir / 'candidates.txt'
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(target_codes) + '\n')
        console.print(f'  候选股(txt): {txt_path}', style='green')
        return

    # ── 5. 获取最新价格并计算调仓计划 ────────────────────────────────
    console.print('')
    console.print('  获取最新价格...')
    from guoquant.common.trade.executor import fetch_current_prices
    # 目标持仓 ∪ 当前持仓：两者都需要现价（卖出估价/止损判断）
    all_codes = set(target_codes) | set(raw_positions.keys())
    latest_prices = fetch_current_prices(all_codes)
    console.print(f'  获取到 {len(latest_prices)} 只股票最新价')

    from guoquant.common.trade.strategy import compute_trade_plan, StopLossManager

    # 从文件加载止损状态，或从当前持仓初始化
    if positions_file and Path(positions_file).exists():
        sl_manager = StopLossManager.load(positions_file)
        console.print(f'  从文件加载止损状态: {positions_file}')
    else:
        # 无历史状态：把现有持仓按买入均价注册为初始止损追踪记录
        sl_manager = StopLossManager()
        for code, pos in raw_positions.items():
            if pos['volume'] > 0:
                sl_manager.register_buy(code, pos['avg_price'],
                                        pos['volume'], today)

    # 调仓计划：对比当前持仓与目标持仓，结合止损/资金约束生成买卖指令
    trade_plan = compute_trade_plan(
        raw_positions, target_codes, sl_manager,
        today, latest_prices, total_cash)

    # ── 6. 输出调仓计划 ─────────────────────────────────────────────
    console.print('')
    console.print('─' * 60)
    console.print('  调仓计划')
    console.print('─' * 60)

    current_set = set(raw_positions.keys())
    target_set = set(target_codes)
    # 当前持仓 ∩ 目标持仓（仅用于展示；止损触发的交集股实际仍会被卖出，
    # 以 trade_plan 的 stop_loss_codes 为准）
    hold_set = current_set & target_set

    # trade_plan['sell'] 元素为 (code, volume, reason)：reason ∈ {stop_loss
    # （止损）, rebalance（调出目标池/超配调仓）}；trade_plan['buy'] 为
    # (code, volume, target_pct)，target_pct 为等权目标权重
    if trade_plan['stop_loss_codes']:
        console.print(f'  ⚠ 止损触发: '
                       f'{", ".join(trade_plan["stop_loss_codes"])}',
                       style='yellow bold')

    if trade_plan['sell']:
        console.print(f'\n  卖出 ({len(trade_plan["sell"])} 笔):')
        for code, vol, reason in trade_plan['sell']:
            price = latest_prices.get(code, 0)
            console.print(f'    {code}  {vol}股  '
                           f'≈{vol*price:,.0f}元  ({reason})')

    if trade_plan['buy']:
        console.print(f'\n  买入 ({len(trade_plan["buy"])} 笔):')
        for code, vol, target_pct in trade_plan['buy']:
            price = latest_prices.get(code, 0)
            console.print(f'    {code}  {vol}股  '
                           f'≈{vol*price:,.0f}元  '
                           f'(目标权重={target_pct:.1%})')

    if hold_set:
        console.print(f'\n  继续持有 ({len(hold_set)} 只):')
        for code in sorted(hold_set):
            pos = raw_positions.get(code, {})
            price = latest_prices.get(code, 0)
            mv = pos.get('volume', 0) * price
            sl_price = sl_manager.get_stop_price(code, today)
            console.print(f'    {code}  {pos.get("volume",0)}股  '
                           f'≈{mv:,.0f}元'
                           + (f'  止损价={sl_price:.2f}' if sl_price else ''))

    if not trade_plan['sell'] and not trade_plan['buy']:
        console.print('  无需调仓，持仓不变')

    console.print('─' * 60)

    # ── 7. 执行 ─────────────────────────────────────────────────────
    if dry_run:
        console.print('')
        console.print('  [模拟模式] 未实际下单。加 --dry-run 可继续模拟。', style='yellow bold')
    else:
        if not trade_plan['sell'] and not trade_plan['buy']:
            console.print('  无需执行订单')
        else:
            console.print('')
            console.print('  执行订单...')

            result = executor.execute_orders(
                trade_plan['sell'],
                trade_plan['buy'],
                verbose_cb=lambda stage, info: (
                    console.print(f'    [{stage}] {info}')
                ),
            )
            console.print(f'  卖出 {result["sold"]} 笔, '
                           f'买入 {result["bought"]} 笔', style='green')
            if result['errors']:
                for err in result['errors']:
                    console.print(f'    {err}', style='red bold')

            # 更新止损追踪：普通调仓卖出且清仓（剩余股数 ≤0）的移除追踪记录；
            # 止损卖单（reason=stop_loss）不移除——记录保留，由后续 watch 看盘
            # 轮次按实际持仓清理；计划买入的单按最新价注册（未校验成交回报）。
            for code, vol, reason in trade_plan['sell']:
                if reason == 'stop_loss':
                    continue
                new_vol = raw_positions.get(code, {}).get('volume', 0) - vol
                if new_vol <= 0:
                    sl_manager.register_sell(code)
            for code, vol, target_pct in trade_plan['buy']:
                price = latest_prices.get(code, 0)
                if price > 0 and vol > 0:
                    sl_manager.register_buy(code, price, vol, today)

            # 保存止损状态
            _save_positions_file = positions_file or str(output_dir / 'stop_loss_state.json')
            sl_manager.save(_save_positions_file)
            console.print(f'  止损状态已保存: {_save_positions_file}', style='green')

            executor.disconnect()

    # ── 8. 保存报告 ─────────────────────────────────────────────────
    # trade_plan.json：完整记录本次调仓决策（目标/买卖/止损/持仓），供复盘与审计
    report = {
        'date': today.isoformat(),
        'strategy': strategy_name,
        'top_n': top_n,
        'total_cash': total_cash,
        'dry_run': dry_run,
        'target_codes': target_codes,
        'target_scores': {c: scores.get(c, 0) for c in target_codes},
        'trade_plan': {
            'sell': [{'code': c, 'volume': v, 'reason': r}
                     for c, v, r in trade_plan['sell']],
            'buy': [{'code': c, 'volume': v, 'target_pct': p}
                    for c, v, p in trade_plan['buy']],
            'stop_loss_codes': trade_plan['stop_loss_codes'],
            'hold_codes': list(hold_set),
        },
        'current_positions': {
            c: {'volume': p['volume'], 'avg_price': p['avg_price']}
            for c, p in raw_positions.items() if p['volume'] > 0
        },
    }

    report_path = output_dir / 'trade_plan.json'
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2,
                  default=str)
    console.print(f'\n  调仓报告已保存: {report_path}', style='green')
