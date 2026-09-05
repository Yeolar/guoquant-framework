"""回测管理命令

基于 backtrader 对选股策略做历史回测；支持单策略模式与 ``--benchmark`` 批量
对比。

用法示例：

.. code-block:: text

    python -m guoquant.cli backtest --strategy stock_strength_strategy \\
        --root 260325/fetched_data --start 250101 --end 260325 \\
        --top-n 10 --rebalance 5 --capital 1000000

依赖：``guoquant.common.backtest.runner.BacktestRunner`` （回测引擎）、
``guoquant.common.backtest.scorer`` （打分预计算编排；``registered_strategies``
注册表本体在 ``guoquant.common.strategy``，scorer 侧再导出）。

数据约定：``root`` 指向某日期的 ``fetched_data`` 目录；回测读取其
``quote/d/stock/`` 日线行情，``stock_flow_strategy`` （``needs_flow``）额外需要
``flow/d/`` 资金流与 ``dist/`` 筹码快照；日期参数统一为 YYMMDD 格式。
"""
from datetime import date
from pathlib import Path

import typer

from guoquant.common.log import console
from guoquant.common.utils import outp, todate


def command(
    # 不在此固定 choices：策略在 guoquant/strategies/ 下注册（含用户新增模块）
    # 后即可传函数全名，有效性在下方按 registered_strategies 动态校验。
    strategy: str = typer.Option(
        'stock_strength_strategy',
        '--strategy',
        help='回测策略（打分核心函数全名，见 guoquant.strategies），'
             'default=stock_strength_strategy。stock_flow_strategy 需 '
             'flow+dist 数据，其余仅需 OHLCV',
    ),
    root: str = typer.Option(
        '{d:%y%m%d}/fetched_data',
        '--root',
        help='数据根目录，default={d:%%y%%m%%d}/fetched_data',
    ),
    start: str = typer.Option(
        None,
        '--start',
        help='回测开始日期，格式 YYMMDD，default=数据最早日期',
    ),
    end: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--end',
        help=f'回测结束日期，格式 YYMMDD，default={date.today():%y%m%d}',
    ),
    top_n: int = typer.Option(
        10,
        '--top-n',
        help='同时持仓只数，default=10',
    ),
    rebalance: int = typer.Option(
        5,
        '--rebalance',
        help='换仓频率（交易日数），default=5',
    ),
    capital: float = typer.Option(
        1_000_000,
        '--capital',
        help='初始资金，default=1000000',
    ),
    stop_loss: float = typer.Option(
        0.08,
        '--stop-loss',
        help='持仓止损阈值（从买入均价的回撤比例），0 表示禁用，default=0.08',
    ),
    benchmark: bool = typer.Option(
        False,
        '--benchmark',
        help='批量回测全部 9 个策略并输出收益对比报告',
    ),
    include_flow: bool = typer.Option(
        False,
        '--include-flow',
        help='（配合 --benchmark）包含 flow 策略（需 flow+dist 数据）',
    ),
    output: str = typer.Option(
        '{d:%y%m%d}/backtest',
        '--output',
        help='输出目录，default={d:%%y%%m%%d}/backtest',
    ),
    date: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--date',
        help=f'用于 root/output 模板的日期，default={date.today():%y%m%d}',
    ),
) -> None:
    """基于 backtrader 对选股策略进行历史回测，支持单策略与批量对比两种模式。

    单策略模式对 ``strategy`` 打分并按固定频率换仓：在终端打印收益报告，并把
    成交明细 CSV 与资金曲线图保存到 ``output`` 目录。``--benchmark`` 模式遍历
    注册表内全部策略（含用户新增，有效性动态校验）逐一独立回测，在终端打印按
    总收益率降序的汇总表，并把每个策略的成交明细分别写入
    ``output/benchmark/<策略名>/`` 目录。

    参数说明（与 ``BacktestRunner`` 一一对应）：

    - ``root`` / ``start`` / ``end``：数据根目录与回测区间（YYMMDD；``start``
      缺省用数据最早日期）；
    - ``top_n``：同时持仓只数上限；``rebalance``：每隔多少个交易日换仓一次；
    - ``stop_loss``：自买入均价的回撤比例触发止损（0 表示禁用）；
    - ``include_flow``：仅在 ``--benchmark`` 模式下有意义。注意：注册表以函数
      全名为键（flow 策略注册名为 ``stock_flow_strategy``，无 ``'flow'`` 短名
      条目），该开关当前不改变批量名单——``stock_flow_strategy`` 缺 flow/dist
      数据时由其引擎入口直接返回空打分（不报错，见
      ``common.strategy.register_strategy``）。

    Args:
        strategy (str): 回测策略（打分核心函数全名，见 ``guoquant.strategies``），
            default=``stock_strength_strategy``；``stock_flow_strategy`` 需
            flow+dist 数据，其余仅需 OHLCV。
        root (str): 数据根目录，default=``{d:%y%m%d}/fetched_data``。
        start (str): 回测开始日期（YYMMDD），default=数据最早日期。
        end (str): 回测结束日期（YYMMDD），default=今天。
        top_n (int): 同时持仓只数，default=10。
        rebalance (int): 换仓频率（交易日数），default=5。
        capital (float): 初始资金，default=1000000。
        stop_loss (float): 持仓止损阈值（自买入均价的回撤比例），0 表示禁用，
            default=0.08。
        benchmark (bool): 批量回测全部策略并输出收益对比报告。
        include_flow (bool): 配合 ``--benchmark`` 包含 flow 策略（需 flow+dist
            数据）。
        output (str): 输出目录，default=``{d:%y%m%d}/backtest``。
        date (str): 用于 ``root`` / ``output`` 模板的日期（YYMMDD），
            default=今天。
    """
    from guoquant.common.backtest.runner import BacktestRunner
    from guoquant.common.backtest.scorer import get_strategy_names, registered_strategies

    strategy_name = strategy
    # 策略有效性按注册表动态校验：在 strategies/ 下新增模块并 @register_strategy
    # 注册（见 user_strategies.py 示例）后即可用
    if strategy_name not in registered_strategies:
        console.print(
            f'Error: 未知策略: {strategy_name}，可用: {", ".join(get_strategy_names())}',
            style='red bold',
        )
        raise typer.Exit(1)

    d = todate(date)
    data_root = outp(root.format(d=d))
    output_dir = outp(output.format(d=d), is_dir=True)

    # 日期模板说明：{d:%y%m%d} 由 --date 参数填充，root/output 共享同一交易日，
    # 保证"数据目录"与"结果目录"落在同一个日期下
    end_date = todate(end)
    start_date = todate(start) if start else None

    top_n = top_n
    rebalance_freq = rebalance
    stop_loss = stop_loss
    initial_cash = capital

    if not data_root.exists():
        console.print(f'Error: 数据目录不存在: {data_root}', style='red bold')
        raise typer.Exit(1)

    # ── Benchmark 模式 ────────────────────────────────────────────
    if benchmark:
        # 注册名均为函数全名（如 stock_flow_strategy），注册表内不存在 'flow'
        # 短名条目，此过滤当前不会排除任何策略：stock_flow_strategy 缺
        # flow/dist 数据时由其引擎入口直接返回空打分（不报错，见 common.strategy）
        strategies = [s for s in get_strategy_names()
                      if include_flow or s != 'flow']
        console.print(
            f'[批量回测] 共 {len(strategies)} 个策略  数据={data_root}')
        console.print(
            f'  持仓={top_n}只  换仓频率={rebalance_freq}日  '
            f'止损={stop_loss:.0%}  初始资金={initial_cash:,.0f}')
        console.print('')

        results = []
        for i, name in enumerate(strategies):
            try:
                # 同一份数据、同一组参数，仅换策略打分，逐个独立回测
                runner = BacktestRunner(
                    data_root=data_root,
                    strategy_name=name,
                    initial_cash=initial_cash,
                    top_n=top_n,
                    rebalance_freq=rebalance_freq,
                    stop_loss=stop_loss,
                )
                r = runner.run(start_date=start_date, end_date=end_date)
                results.append(r)
                console.print(
                    f'  [{i+1}/{len(strategies)}] {name:>22s}  '
                    f'Return={r.total_return:+.2%}  '
                    f'CAGR={r.cagr:+.1%}  '
                    f'MaxDD={r.max_drawdown:.1%}  '
                    f'Sharpe={r.sharpe:.2f}')
            except Exception as e:
                # 单个策略失败不中断整体对比，记录后继续
                console.print(f'FAILED: {e}')

        # 打印汇总
        _print_benchmark_summary(results)

        # 保存结果：每个策略的成交明细单独存到 benchmark/<策略名>/ 下
        benchmark_dir = Path(output_dir) / 'benchmark'
        for r in results:
            r.save_trades(benchmark_dir / r.strategy_name)
        console.print(f'  明细已保存: {benchmark_dir}', style='green')
        return

    # ── 单策略模式 ─────────────────────────────────────────────────
    console.print(f'[回测] 策略={strategy_name}  数据={data_root}')
    console.print(f'       持仓={top_n}只  换仓频率={rebalance_freq}日  '
                   f'止损={stop_loss:.0%}  初始资金={initial_cash:,.0f}')

    def progress(stage, info):
        # 回测引擎分阶段进度回调，按 stage 分发打印：'scored' 时
        # info=(日期, 该日有效股票数) 元组，其余 stage 的 info 为单值
        # （数据根路径或数量等）
        if stage == 'loading_quote':
            console.print(f'  加载行情数据: {info}')
        elif stage == 'loading_flow':
            console.print(f'  加载资金流数据: {info}')
        elif stage == 'scoring':
            console.print(f'  预计算分数（共 {info} 个换仓日）...')
        elif stage == 'scored':
            d_val, n = info
            console.print(f'    {d_val}  →  {n} 只有效股票')
        elif stage == 'adding_feeds':
            console.print(f'  添加 backtrader 数据 feed（最多 {info} 只）...')
        elif stage == 'running':
            console.print(f'  运行回测（{info} 只股票）...')

    try:
        runner = BacktestRunner(
            data_root=data_root,
            strategy_name=strategy_name,
            initial_cash=initial_cash,
            top_n=top_n,
            rebalance_freq=rebalance_freq,
            stop_loss=stop_loss,
        )
        result = runner.run(
            start_date=start_date,
            end_date=end_date,
            verbose_cb=progress,
        )
    except ValueError as e:
        # 常见于数据不足/区间为空等输入性错误，直接以错误信息退出
        console.print(f'Error: {str(e)}', style='red bold')
        raise typer.Exit(1)

    # 打印报告
    result.print_report(console)

    # 保存 CSV
    trades_path = result.save_trades(output_dir)
    if trades_path.exists():
        console.print(f'  交易记录已保存: {trades_path}', style='green')

    # 保存图表
    try:
        plot_path = result.save_plot(output_dir)
        console.print(f'  资金曲线已保存: {plot_path}', style='green')
    except Exception as e:
        console.print(f'  图表生成失败: {e}', style='yellow bold')


# ── Benchmark 辅助 ──────────────────────────────────────────────── #

def _print_benchmark_summary(results):
    """在终端打印 benchmark 汇总表。

    按总收益率降序排列；成交笔数按 ``trade_log`` 中 ``size != 0`` 的记录统计
    （剔除空仓/调整类记录）。

    Args:
        results (list): 各策略的回测结果对象列表，含 ``total_return`` /
            ``cagr`` / ``max_drawdown`` / ``sharpe`` / ``trade_log`` /
            ``strategy_name`` 等字段。
    """
    print()
    print('=' * 92)
    print(f'  {"策略":>22s}  {"Return":>8s}  {"CAGR":>8s}  '
          f'{"MaxDD":>8s}  {"Sharpe":>7s}  {"成交笔数":>7s}')
    print('-' * 92)

    for r in sorted(results, key=lambda x: x.total_return, reverse=True):
        trades = sum(
            1 for t in r.trade_log if t.get('size', 0) != 0)
        print(
            f'  {r.strategy_name:>22s}  '
            f'{r.total_return:>+7.1%}  '
            f'{r.cagr:>+7.1%}  '
            f'{r.max_drawdown:>+7.1%}  '
            f'{r.sharpe:>6.2f}  '
            f'{trades:>7d}')
    print('=' * 92)
    print()
