"""极端恐慌逆向策略回测管理命令

检测市场恐慌事件并模拟"逆向抄底"：恐慌信号触发后于下一交易日买入，持有
``hold`` 个交易日后卖出（成交时点与统计口径见 ``panic_runner``）。
依赖：``guoquant.common.backtest.panic_runner`` （恐慌检测与回测引擎）。

用法示例：

.. code-block:: text

    # 单一参数回测
    python -m guoquant.cli panic_backtest --root 260325/fetched_data \\
        --hold 5 --top-n 10 --capital 1000000

    # 参数扫描（网格搜索）
    python -m guoquant.cli panic_backtest --root 260325/fetched_data --sweep

    # 指定日期范围
    python -m guoquant.cli panic_backtest --root 260325/fetched_data \\
        --start 240101 --end 260325 --hold 5
"""
from datetime import date
from pathlib import Path

import typer

from guoquant.common.log import console
from guoquant.common.utils import outp, todate


def command(
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
    hold: int = typer.Option(
        5,
        '--hold',
        help='持有天数（恐慌触发后持有几个交易日），default=5',
    ),
    top_n: int = typer.Option(
        10,
        '--top-n',
        help='每批持仓只数，default=10',
    ),
    stop_loss: float = typer.Option(
        0.10,
        '--stop-loss',
        help='止损阈值（相对成本的亏损），0=禁用，default=0.10',
    ),
    deploy_pct: float = typer.Option(
        1.0,
        '--deploy-pct',
        help='每次信号投入可用资金的比例，default=1.0',
    ),
    capital: float = typer.Option(
        1_000_000,
        '--capital',
        help='初始资金，default=1000000',
    ),
    output: str = typer.Option(
        '{d:%y%m%d}/panic_backtest',
        '--output',
        help='输出目录，default={d:%%y%%m%%d}/panic_backtest',
    ),
    date: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--date',
        help=f'用于 root/output 模板的日期，default={date.today():%y%m%d}',
    ),
    sweep: bool = typer.Option(
        False,
        '--sweep',
        help='参数网格搜索：测试 hold=[3,5,7,10] × top_n=[5,10]',
    ),
    min_stocks: int = typer.Option(
        100,
        '--min-stocks',
        help='恐慌检测最少需要的有效股票数，default=100',
    ),
    min_gap: int = typer.Option(
        5,
        '--min-gap',
        help='两次恐慌事件间最小间隔天数（去重），default=5',
    ),
) -> None:
    """检测市场恐慌事件并模拟"逆向抄底"回测。

    恐慌信号触发后于下一交易日开盘买入、持有 ``hold`` 个交易日后卖出（见
    ``panic_runner`` 的 ``PositionBatch`` / ``simulate_batch``）。恐慌检测参数：
    ``min_stocks`` （当日有效股票数下限，太少视为数据不全）、``min_gap`` （两次
    恐慌事件的最小间隔，用于把同一轮下跌去重为一次事件）。

    分支说明：

    - ``--sweep``：按 ``hold ∈ {3, 5, 7, 10}`` × ``top_n ∈ {5, 10}`` 网格扫描
      并终端汇总对比，各组交易明细/每日净值/资金曲线分别存入
      ``<output>/sweep/hold{d}d_top{n}/``；
    - 单次回测：终端打印收益报告与恐慌事件明细（前 20 个），并把交易明细、
      每日净值与资金曲线保存到 ``output`` 目录。

    Args:
        root (str): 数据根目录，default=``{d:%y%m%d}/fetched_data``。
        start (str): 回测开始日期（YYMMDD），default=数据最早日期。
        end (str): 回测结束日期（YYMMDD），default=今天。
        hold (int): 持有天数（恐慌触发后持有几个交易日），default=5。
        top_n (int): 每批持仓只数，default=10。
        stop_loss (float): 止损阈值（相对成本的亏损比例），0 表示禁用，
            default=0.10。
        deploy_pct (float): 每次信号投入可用资金的比例，default=1.0。
        capital (float): 初始资金，default=1000000。
        output (str): 输出目录，default=``{d:%y%m%d}/panic_backtest``。
        date (str): 用于 ``root`` / ``output`` 模板的日期（YYMMDD），
            default=今天。
        sweep (bool): 参数网格搜索：测试 ``hold=[3,5,7,10]`` ×
            ``top_n=[5,10]``。
        min_stocks (int): 恐慌检测最少需要的有效股票数，default=100。
        min_gap (int): 两次恐慌事件间最小间隔天数（去重），default=5。
    """
    from guoquant.common.backtest.panic_runner import (
        run_panic_backtest, run_panic_sweep, print_sweep_summary,
    )

    d = todate(date)
    data_root = outp(root.format(d=d))
    output_dir = outp(output.format(d=d), is_dir=True)

    end_date = todate(end)
    start_date = todate(start) if start else None

    hold_days = hold
    top_n = top_n
    stop_loss = stop_loss
    deploy_pct = deploy_pct
    initial_cash = capital
    min_stocks = min_stocks
    min_gap = min_gap

    if not data_root.exists():
        console.print(f'Error: 数据目录不存在: {data_root}', style='red bold')
        raise typer.Exit(1)

    console.print(f'[恐慌逆向回测] 数据={data_root}')
    console.print(f'  持有天数={hold_days}d  每批持仓={top_n}只  '
                   f'止损={stop_loss:.0%}  资金使用率={deploy_pct:.0%}  '
                   f'初始资金={initial_cash:,.0f}')

    if sweep:
        console.print('\n  ⏳ 参数网格扫描中...')
        # 网格：持有天数 × 每批持仓数 共 8 组参数，逐一回测并汇总
        results = run_panic_sweep(
            data_root=data_root,
            hold_days_list=(3, 5, 7, 10),
            top_n_list=(5, 10),
            stop_loss=stop_loss,
            initial_cash=initial_cash,
            start_date=start_date,
            end_date=end_date,
            min_stocks=min_stocks,
            min_gap_days=min_gap,
        )
        print_sweep_summary(results)

        # 保存所有结果到统一目录：sweep/hold{d}d_top{n}/ 下分别存交易、净值、曲线
        results_dir = Path(output_dir) / 'sweep'
        for r in results:
            sub = results_dir / f'hold{r.hold_days}d_top{r.top_n}'
            trades_path = r.save_trades(sub)
            nav_path = r.save_daily_nav(sub)
            try:
                plot_path = r.save_plot(sub)
            except Exception as e:
                console.print(f'  图表生成失败 ({r.hold_days}d_{r.top_n}n): {e}', style='yellow bold')
        console.print(f'  网格扫描结果已保存到: {results_dir}', style='green')
        return

    # 单一回测
    def progress(stage, info):
        # 恐慌回测引擎的分阶段进度回调，stage ∈ {loading, matrix, detecting,
        # signals, trading, progress, done, nosignals}，info 为对应阶段描述
        if stage == 'loading':
            console.print(f'  加载行情数据: {info}')
        elif stage == 'matrix':
            console.print(f'  构建收益率矩阵: {info}')
        elif stage == 'detecting':
            console.print(f'  检测恐慌事件...')
        elif stage == 'signals':
            console.print(f'  检测结果: {info}')
        elif stage == 'trading':
            console.print(f'  模拟交易: {info}')
        elif stage == 'progress':
            console.print(f'    {info}')
        elif stage == 'done':
            console.print(f'  回测完成: {info}')
        elif stage == 'nosignals':
            console.print(f'  ⚠ {info}')

    try:
        result = run_panic_backtest(
            data_root=data_root,
            hold_days=hold_days,
            top_n=top_n,
            stop_loss=stop_loss,
            deploy_pct=deploy_pct,
            initial_cash=initial_cash,
            start_date=start_date,
            end_date=end_date,
            min_stocks=min_stocks,
            min_gap_days=min_gap,
            verbose_cb=progress,
        )
    except ValueError as e:
        console.print(f'Error: {str(e)}', style='red bold')
        raise typer.Exit(1)

    # 打印报告
    result.print_report()

    # 输出每个恐慌事件的详情
    if result.signals:
        console.print('  恐慌事件明细（前20个）:')
        for i, sig in enumerate(result.signals[:20]):
            # sig 字段：date=触发日, avg_return=当日横截面平均涨跌幅,
            # decline_5pct_ratio=当日跌幅超5%的股票占比,
            # avg_5d_return=截至触发日的5日滚动累积收益（引擎口径，非未来收益）,
            # total_stocks=当日有效股票数, label=事件类型（普跌/极端暴跌/连续阴跌）
            console.print(
                f'    {i+1:>3d}. {sig.date}  [{sig.label}]  '
                f'avg_ret={sig.avg_return:+.2%}  '
                f'd5_ratio={sig.decline_5pct_ratio:.1%}  '
                f'5d_ret={sig.avg_5d_return:+.2%}  '
                f'n={sig.total_stocks}'
            )
        if len(result.signals) > 20:
            console.print(f'    ... 还有 {len(result.signals) - 20} 个事件')

    # 保存结果
    trades_path = result.save_trades(Path(output_dir))
    if trades_path.exists():
        console.print(f'  交易明细已保存: {trades_path}', style='green')

    nav_path = result.save_daily_nav(Path(output_dir))
    if nav_path.exists():
        console.print(f'  每日净值已保存: {nav_path}', style='green')

    try:
        plot_path = result.save_plot(Path(output_dir))
        console.print(f'  资金曲线已保存: {plot_path}', style='green')
    except Exception as e:
        console.print(f'  图表生成失败: {e}', style='yellow bold')
