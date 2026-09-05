"""因子 IC 分析管理命令

对预定义因子清单（``ic_analyzer.FACTOR_DEFS``：OHLCV + 资金流因子）计算
Spearman IC，评估各因子对未来收益的预测能力。输出：IC 时序 CSV、汇总统计
CSV、三张可视化图表。
依赖：``guoquant.common.backtest.ic_analyzer`` （IC 计算与结果对象）、
``guoquant.common.quote`` （``load_quote_dfs`` / ``load_flow_dfs`` 整表加载）、
``guoquant.common.backtest.data`` （``get_all_trading_dates`` 交易日汇总）。

IC 语义：每个截面 t 上，因子值（仅用 t 及以前的数据）与 t→t+forward 未来收益
的 Spearman 秩相关；IC 均值为负/正分别表示反向/正向预测能力。
``IC_IR = IC 均值 / IC 标准差`` （衡量稳定性），t 统计量 = IC 均值 / 标准误。

用法示例：

.. code-block:: text

    # 使用最新数据，分析 2024 年至今
    python -m guoquant.cli ic_analysis --root 260404/fetched_data \\
        --start 240101

    # 只分析 OHLCV 因子（不加载资金流，速度更快）
    python -m guoquant.cli ic_analysis --root 260404/fetched_data --no-flow

    # 自定义预测窗口和截面频率
    python -m guoquant.cli ic_analysis --root 260404/fetched_data \\
        --forward 5,10 --freq 3 --min-stocks 50
"""
import numpy as np
from datetime import date

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
        help='分析起始日期，格式 YYMMDD（default=数据最早日期）',
    ),
    end: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--end',
        help=f'分析截止日期，格式 YYMMDD，default={date.today():%y%m%d}',
    ),
    forward: str = typer.Option(
        '5,10,20',
        '--forward',
        help='预测窗口（交易日），逗号分隔，default=5,10,20',
    ),
    freq: int = typer.Option(
        5,
        '--freq',
        help='截面采样频率：每隔多少交易日取一个截面，default=5',
    ),
    min_stocks: int = typer.Option(
        30,
        '--min-stocks',
        help='单截面最少有效股票数，不足则跳过该截面，default=30',
    ),
    no_flow: bool = typer.Option(
        False,
        '--no-flow',
        help='跳过资金流因子（不加载 flow 数据，速度更快）',
    ),
    output: str = typer.Option(
        '{d:%y%m%d}/ic_analysis',
        '--output',
        help='输出目录，default={d:%%y%%m%%d}/ic_analysis',
    ),
    date: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--date',
        help=f'用于 root/output 路径模板的日期，default={date.today():%y%m%d}',
    ),
) -> None:
    """对所有注册因子做 IC（信息系数）分析，评估各因子对未来收益的预测能力。

    在 ``[start, end]`` 区间内每隔 ``freq`` 个交易日取一个截面，计算各因子与
    ``forward`` 各预测窗口未来收益的 Spearman IC；保存 IC 时序 CSV、汇总统计
    CSV 与三张可视化图表到 ``output`` 目录，并在终端打印按 ``IC_IR`` 降序的
    因子汇总报告。单个截面有效股票数不足 ``min_stocks`` 时跳过该截面（避免
    小样本噪声）。

    Args:
        root (str): 数据根目录，default=``{d:%y%m%d}/fetched_data``。
        start (str): 分析起始日期（YYMMDD），default=数据最早日期。
        end (str): 分析截止日期（YYMMDD），default=今天。
        forward (str): 预测窗口（交易日），逗号分隔多个窗口，
            default=``5,10,20``。
        freq (int): 截面采样频率：每隔多少交易日取一个截面，default=5。
        min_stocks (int): 单截面最少有效股票数，不足则跳过该截面，
            default=30。
        no_flow (bool): 跳过资金流因子（不加载 flow 数据，速度更快）。
        output (str): 输出目录，default=``{d:%y%m%d}/ic_analysis``。
        date (str): 用于 ``root`` / ``output`` 路径模板的日期（YYMMDD），
            default=今天。
    """
    from guoquant.common.quote import load_quote_dfs, load_flow_dfs
    from guoquant.common.backtest.data import get_all_trading_dates
    from guoquant.common.backtest.ic_analyzer import run_ic_analysis, FACTOR_DEFS

    d          = todate(date)
    data_root  = outp(root.format(d=d))
    output_dir = outp(output.format(d=d), is_dir=True)
    end_date   = todate(end)
    start_date = todate(start) if start else None
    fwd_list   = [int(x) for x in forward.split(',')]
    sample_freq = freq
    min_stocks  = min_stocks
    use_flow    = not no_flow

    if not data_root.exists():
        console.print(f'Error: 数据目录不存在: {data_root}', style='red bold')
        raise typer.Exit(1)

    # ── 1. 加载数据 ───────────────────────────────────────────────────────
    console.print('[1/3] 加载行情数据...')
    quote_dfs = load_quote_dfs(data_root)
    if not quote_dfs:
        console.print('Error: 未找到行情数据', style='red bold')
        raise typer.Exit(1)
    console.print(f'      共 {len(quote_dfs)} 只股票')

    flow_dfs = None
    if use_flow:
        # flow 数据可选：资金流因子需要，否则跳过以加快分析
        console.print('      加载资金流数据...')
        flow_dfs = load_flow_dfs(data_root)
        console.print(f'      资金流：{len(flow_dfs)} 只')

    # ── 2. 估算截面数量 ───────────────────────────────────────────────────
    all_dates = get_all_trading_dates(quote_dfs, start_date, end_date)
    max_fwd = max(fwd_list)
    n = len(all_dates)
    # 每个截面 i 需要 i+max_fwd < n（未来收益窗口完整），按 sample_freq 步长取截面
    total = sum(1 for i in range(0, n, sample_freq) if i + max_fwd < n)

    # FACTOR_DEFS 为 (因子名, 参数tuple, 数据类型, 最小历史窗口) 四元组；
    # dt ∈ {'ohlcv','flow'} 决定该因子属于哪一类（仅用于下面计数显示）
    ohlcv_count = sum(1 for _, _, dt, _ in FACTOR_DEFS if dt == 'ohlcv')
    flow_count  = sum(1 for _, _, dt, _ in FACTOR_DEFS if dt == 'flow')

    console.print(f'\n[2/3] IC 分析')
    console.print(f'      预测窗口  : {fwd_list} 交易日')
    console.print(f'      截面频率  : 每 {sample_freq} 个交易日')
    console.print(f'      截面总数  : {total}')
    console.print(f'      OHLCV 因子: {ohlcv_count} 个')
    console.print(f'      资金流因子: {flow_count} 个（{"启用" if use_flow else "跳过"}）')
    console.print(f'      最少股票  : {min_stocks} 只/截面\n')

    # 进度打印（每 10% 汇报一次）
    _milestones = set(range(0, total, max(1, total // 10)))
    _milestones.add(total - 1)

    def verbose_cb(i, total_, current_date):
        # 分析引擎回调：i 为当前截面序号，current_date 为该截面日期
        if i in _milestones:
            pct = (i + 1) / total_ * 100
            console.print(f'      [{pct:5.1f}%] {current_date}')

    try:
        ic_result = run_ic_analysis(
            quote_dfs=quote_dfs,
            flow_dfs=flow_dfs,
            start_date=start_date,
            end_date=end_date,
            forward_days_list=fwd_list,
            sample_freq=sample_freq,
            min_stocks=min_stocks,
            verbose_cb=verbose_cb,
        )
    except ValueError as e:
        # 常见于区间内无完整截面/股票数不足，属输入性问题
        console.print(f'Error: {str(e)}', style='red bold')
        raise typer.Exit(1)

    # ── 3. 保存结果 ───────────────────────────────────────────────────────
    console.print(f'\n[3/3] 保存结果...')
    summary_path = ic_result.save(output_dir)
    console.print(f'  IC 汇总 CSV : {summary_path}', style='green')
    for fwd in fwd_list:
        console.print(f'  IC 时序 CSV : {output_dir}/ic_ts_{fwd}d.csv', style='green')

    try:
        ic_result.plot(output_dir)
        console.print(f'  图表目录    : {output_dir}/', style='green')
    except Exception as e:
        console.print(f'  图表生成失败: {e}', style='yellow bold')

    # ── 打印汇总报告 ──────────────────────────────────────────────────────
    _print_report(ic_result)


def _print_report(ic_result):
    """在终端打印各因子 IC 汇总报告。

    按预测窗口分组，窗口内按 ``IC_IR`` 绝对值降序排列（NaN 因子补在末尾）。
    评价标准：``|IC_mean| > 0.04`` 视为有效，``|IC_IR| >= 1.0`` 两星、
    ``>= 0.5`` 一星，``|t| >= 2.0`` 标注统计显著；任一统计量为空/非有限值时该
    因子标注为数据不足。

    Args:
        ic_result: IC 分析结果对象（含 ``summary()`` 与 ``forward_days_list``
            属性）。
    """
    summary = ic_result.summary()
    fwd_list = ic_result.forward_days_list

    console.print('\n' + '═' * 76)
    console.print('  因子 IC 分析报告')
    console.print('  评价标准：|IC_mean|>0.04 有效  IC_IR>0.5 ★  IC_IR>1.0 ★★  |t|>2.0 显著')
    console.print('═' * 76)

    for fwd in fwd_list:
        console.print(f'\n  ◆ 预测窗口 {fwd} 交易日')
        console.print(f"  {'因子':<36}{'IC均值':>8}{'IC_IR':>8}{'t统计量':>9}{'正向率':>8}  评级")
        console.print('  ' + '─' * 72)

        # 按 IC_IR 绝对值降序：IC_IR 兼顾均值与稳定性，是主要排序依据
        col_ir = f'{fwd}d_IC_IR'
        valid = summary[col_ir].dropna()
        sorted_factors = valid.abs().sort_values(ascending=False).index.tolist()
        # 补充 NaN 的因子放末尾
        nan_factors = [f for f in summary.index if f not in sorted_factors]

        for fac in sorted_factors + nan_factors:
            ic_mean   = summary.loc[fac, f'{fwd}d_IC_mean']
            ic_ir     = summary.loc[fac, f'{fwd}d_IC_IR']
            t_stat    = summary.loc[fac, f'{fwd}d_t_stat']
            pos_ratio = summary.loc[fac, f'{fwd}d_pos_ratio']

            # 任一统计量为空/非有限值 → 该因子在该窗口数据不足，单独标注
            if not all(np.isfinite([v if v is not None else float('nan')
                                    for v in [ic_mean, ic_ir, t_stat, pos_ratio]])):
                console.print(f'  {fac:<36}{"(数据不足)":>34}')
                continue

            # 星级按 |IC_IR| 评级：>=1.0 两星，>=0.5 一星；* 表示 |t|>=2.0 显著
            stars = ''
            if abs(ic_ir) >= 1.0:
                stars = '★★'
            elif abs(ic_ir) >= 0.5:
                stars = '★ '

            sig = '*' if abs(t_stat) >= 2.0 else ' '

            line = (f'  {fac:<36}'
                    f'{ic_mean:>+8.4f}'
                    f'{ic_ir:>+8.4f}'
                    f'{t_stat:>+8.3f}{sig}'
                    f'{pos_ratio:>8.1%}'
                    f'  {stars}')
            console.print(line)

    console.print('\n' + '═' * 76)
    console.print('  * t统计量后的 * 表示 |t|>2.0（统计显著）')
    console.print('═' * 76 + '\n')
