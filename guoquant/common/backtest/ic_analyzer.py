"""因子 IC（Information Coefficient）分析模块

在截面上计算各因子与未来 N 日收益的 Spearman 秩相关（IC）并做汇总评估，
供 ``guoquant.commands.ic_analysis`` 命令行入口调用。

口径与评价参考：

- IC = Spearman 秩相关（因子截面值, N 日后收益）；IC_IR = IC 均值 / IC
  标准差；
- IC_mean 绝对值大于 0.04 视为有参考价值；IC_IR 大于 0.5 值得关注、大于
  1.0 为强因子；t_stat 绝对值大于 2.0 统计显著（95% 置信）；
- dist 快照因子（``dist_*``）仅有最新快照、无历史时序，IC 分析中已跳过。

与其他模块的关系：

- 被 ``guoquant.commands.ic_analysis`` （命令行入口）调用；
- 交易日由 ``.data.get_all_trading_dates`` 汇总，df→Quote 组装统一走
  ``common.quote.make_quote`` （内部按 ``current_date`` 截断行情）。

无前视偏差说明：

- 因子值仅使用 ``current_date`` 及之前的数据计算（``make_quote`` 按
  ``current_date`` 截断行情）；
- 未来 N 日收益（``compute_forward_returns``）只用于评估因子预测力（IC），
  不参与任何交易决策，因此不构成前视偏差。
"""
import warnings
import numpy as np
import pandas as pd
from scipy.stats import spearmanr, ttest_1samp

from guoquant.common.backtest.data import get_all_trading_dates
from guoquant.common.quote import Quote, make_quote, slice_to_date


# ── 因子定义 ──────────────────────────────────────────────────────────────────
# (因子名, 参数tuple, 数据类型, 最小历史窗口)
# 数据类型: 'ohlcv' | 'flow'

FACTOR_DEFS = [
    # 收益率 / 动量
    ('tail_rise_rate',          (5,),   'ohlcv',  7),
    ('tail_rise_rate',          (10,),  'ohlcv', 12),
    ('tail_rise_rate',          (20,),  'ohlcv', 22),
    ('tail_rise_rate',          (60,),  'ohlcv', 62),
    # 回撤
    ('tail_drawdown',           (10,),  'ohlcv', 10),
    ('tail_drawdown',           (20,),  'ohlcv', 20),
    # 价格位置
    ('tail_price_position',     (20,),  'ohlcv', 20),
    ('tail_price_position',     (60,),  'ohlcv', 60),
    # 布林带宽
    ('tail_boll_bandwidth',     (20,),  'ohlcv', 22),
    # 价格加速度
    ('tail_price_acc',          (),     'ohlcv', 22),
    # 量价相关
    ('tail_vp_corr',            (20,),  'ohlcv', 20),
    # 尾盘强度
    ('tail_close_strength',     (20,),  'ohlcv', 20),
    # 均线多头排列
    ('tail_ma_alignment',       (),     'ohlcv', 62),
    # 成交量类
    ('tail_limitup_count',      (20,),  'ohlcv', 20),
    ('tail_turnover_rate_avg',  (20,),  'ohlcv', 20),
    ('tail_amount_avg',         (20,),  'ohlcv', 20),
    # 筹码因子
    ('tail_vwap_dist',          (20,),  'ohlcv', 20),
    ('tail_vwap_dist',          (60,),  'ohlcv', 60),
    ('tail_cum_turnover',       (20,),  'ohlcv', 20),
    ('tail_chip_concentration', (20,),  'ohlcv', 20),
    ('tail_control_degree',     (20,),  'ohlcv', 22),
    # 资金流因子（需要 flow 数据）
    ('tail_main_flow_rate',       (5,),  'flow', 5),
    ('tail_main_flow_rate',       (10,), 'flow', 10),
    ('tail_main_flow_rate',       (20,), 'flow', 20),
    ('tail_main_flow_continuity', (10,), 'flow', 10),
    ('tail_main_flow_continuity', (20,), 'flow', 20),
    ('tail_super_dominance',      (20,), 'flow', 20),
    ('tail_flow_acceleration',    (5, 20), 'flow', 20),
]


def factor_label(name, args):
    """按因子名与参数 tuple 拼接生成因子标签。

    Args:
        name (str): 因子名，如 ``tail_rise_rate``。
        args (tuple): 因子参数，可为空 tuple。

    Returns:
        str: 无参数时返回因子名本身；否则返回 ``name`` 与各参数以 ``_``
        连接的结果（如 ``tail_rise_rate_20``）。
    """
    if not args:
        return name
    return '_'.join([name] + [str(a) for a in args])


# ── 核心计算函数 ──────────────────────────────────────────────────────────────

def _make_flow_quote(code, quote_df, flow_df, current_date):
    """构建带资金流的 Quote 对象（不含 dist 快照）。

    统一走 ``quote.make_quote``：按 ``current_date`` 截断行情并组装资金流。
    不使用仅最新值的 dist 快照，避免在 IC 分析中引入前视偏差。

    Args:
        code (str): 股票代码。
        quote_df (pd.DataFrame): 该股行情 DataFrame。
        flow_df (pd.DataFrame): 该股资金流 DataFrame。
        current_date (date): 截面日期，只使用该日及之前的数据。

    Returns:
        Quote | None: 组装好的 Quote 对象；行情不足时可能为 None。
    """
    return make_quote(code, quote_df, current_date, flow_df=flow_df)


def compute_cross_section(quote_dfs, flow_dfs, current_date, factor_defs=None):
    """在单个截面上计算全部股票的全部因子值。

    因子按定义的数据类型分组：OHLCV 因子与资金流因子分别经
    ``quote.make_quote`` 组装后，通过 ``getattr`` 反射调用 Quote 上的同名因子
    方法。数据不足最小历史窗口（``min_w``）的股票该因子留空（不进入结果
    dict）；无效值（None / NaN / inf）一律剔除，避免污染后续 IC 计算。资金流
    因子仅在传入 ``flow_dfs`` 时计算，且不使用 dist 快照。因子值只使用
    ``current_date`` 及之前的数据，无前视偏差。

    Args:
        quote_dfs (dict[str, pd.DataFrame]): 各股票行情。
        flow_dfs (dict[str, pd.DataFrame] | None): 各股票资金流；为 None 时
            跳过资金流因子。
        current_date (date): 截面日期。
        factor_defs (list | None): 因子定义列表，元素为
            ``(因子名, 参数tuple, 数据类型, 最小历史窗口)``，数据类型为
            ``'ohlcv'`` 或 ``'flow'``；默认使用模块级 ``FACTOR_DEFS``。

    Returns:
        dict[str, dict[str, float]]: 因子标签 → {股票代码: 因子值}。
    """
    if factor_defs is None:
        factor_defs = FACTOR_DEFS

    ohlcv_defs = [(n, a, mw) for n, a, dt, mw in factor_defs if dt == 'ohlcv']
    flow_defs  = [(n, a, mw) for n, a, dt, mw in factor_defs if dt == 'flow']

    result = {factor_label(n, a): {} for n, a, *_ in factor_defs}

    # OHLCV 因子：通过 getattr 反射调用 Quote 上的同名因子方法
    for code, df in quote_dfs.items():
        q = make_quote(code, df, current_date)
        if q is None:
            continue
        n_rows = len(q)
        for fname, fargs, min_w in ohlcv_defs:
            if n_rows < min_w:
                continue
            lbl = factor_label(fname, fargs)
            try:
                val = getattr(q, fname)(*fargs)
                # 无效值（None / NaN / inf）一律不进入结果，避免污染 IC 计算
                if val is not None and np.isfinite(float(val)):
                    result[lbl][code] = float(val)
            except Exception:
                pass

    # 资金流因子：需要额外的 flow 数据（无 dist 快照，避免前视偏差）
    if flow_defs and flow_dfs:
        for code in quote_dfs:
            if code not in flow_dfs:
                continue
            q = _make_flow_quote(code, quote_dfs[code], flow_dfs[code], current_date)
            if q is None:
                continue
            n_rows = len(q)
            for fname, fargs, min_w in flow_defs:
                if n_rows < min_w:
                    continue
                lbl = factor_label(fname, fargs)
                try:
                    val = getattr(q, fname)(*fargs)
                    if val is not None and np.isfinite(float(val)):
                        result[lbl][code] = float(val)
                except Exception:
                    pass

    return result


def compute_forward_returns(quote_dfs, current_date, forward_days):
    """计算全部股票自 ``current_date`` 起第 ``forward_days`` 个交易日的收益率。

    使用各股自身行情定位未来窗口，自然跳过停牌期：以 ``current_date`` 当日
    （含）之前最近的交易日收盘价为基准，取其后第 ``forward_days`` 个交易日的
    收盘价，收益 = 未来收盘 / 当日收盘 - 1。只用于评估因子有效性（IC），不是
    交易信号。

    Args:
        quote_dfs (dict[str, pd.DataFrame]): 各股票行情。
        current_date (date): 基准截面日期。
        forward_days (int): 未来持有交易日数（如 5 / 10 / 20）。

    Returns:
        dict[str, float]: 股票代码 → 未来 N 日收益率；当日无有效基准或未来
        窗口超出数据范围的股票不在结果中。
    """
    returns = {}
    for code, df in quote_dfs.items():
        ts = pd.Timestamp(current_date)
        # get_indexer(method='pad')：定位 current_date 当日或之前最近的交易日
        pos = df.index.get_indexer([ts], method='pad')[0]
        if pos < 0:
            continue
        close_curr = df.iloc[pos]['c']
        if close_curr <= 0:
            continue

        exit_pos = pos + forward_days
        if exit_pos >= len(df):
            continue  # 未来窗口超出数据范围，跳过
        close_fut = df.iloc[exit_pos]['c']
        if close_fut <= 0:
            continue

        returns[code] = close_fut / close_curr - 1
    return returns


def compute_ic_row(factor_vals, forward_returns, min_stocks=30):
    """计算单个截面上各因子的 Spearman IC。

    Spearman 秩相关先对因子与收益做秩变换、再求 Pearson 相关，只衡量单调
    关系，对异常值更稳健。某因子与收益的股票交集数小于 ``min_stocks`` 时该
    因子记为 NaN（样本不足不可信）；相关结果非有限值时同样记为 NaN。

    Args:
        factor_vals (dict[str, dict[str, float]]): 因子标签 → {股票代码: 因子值}
            （``compute_cross_section`` 的输出）。
        forward_returns (dict[str, float]): 股票代码 → 未来 N 日收益
            （``compute_forward_returns`` 的输出）。
        min_stocks (int): 单因子最少有效股票数，默认 30。

    Returns:
        dict[str, float]: 因子标签 → 该截面 Spearman IC；样本不足或相关为非
        有限值时值为 NaN。
    """
    ic_row = {}
    for lbl, fv_dict in factor_vals.items():
        common = [c for c in fv_dict if c in forward_returns]
        if len(common) < min_stocks:
            ic_row[lbl] = np.nan
            continue
        fv = [fv_dict[c] for c in common]
        fr = [forward_returns[c] for c in common]
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            ic, _ = spearmanr(fv, fr)
        ic_row[lbl] = float(ic) if np.isfinite(ic) else np.nan
    return ic_row


# ── 主流程 ────────────────────────────────────────────────────────────────────

def run_ic_analysis(
    quote_dfs,
    flow_dfs=None,
    start_date=None,
    end_date=None,
    forward_days_list=(5, 10, 20),
    sample_freq=5,
    min_stocks=30,
    verbose_cb=None,
):
    """运行完整 IC 分析流程。

    先汇总全部行情交易日并按 ``start_date`` / ``end_date`` 过滤，随后自序列
    起每 ``sample_freq`` 个交易日取一个截面（并保证该截面未来
    ``max(forward_days_list)`` 个交易日的收益窗口完整落在数据范围内，避免
    尾部截面缺未来数据）。逐截面计算因子值、各预测窗口的前向收益与 Spearman
    IC，最后按预测窗口组装为 ``ICAnalysisResult``。

    Args:
        quote_dfs (dict[str, pd.DataFrame]): 各股票行情。
        flow_dfs (dict[str, pd.DataFrame] | None): 各股票资金流，可选；默认
            None。
        start_date (date | None): 分析起始日期；默认 None（不限）。
        end_date (date | None): 分析截止日期；默认 None（不限）。
        forward_days_list (tuple[int, ...]): 多个预测窗口（交易日数），默认
            (5, 10, 20)。
        sample_freq (int): 每隔多少交易日取一个截面，默认 5。
        min_stocks (int): 单截面最少有效股票数，默认 30。
        verbose_cb (callable | None): 进度回调，签名
            ``verbose_cb(i, total, current_date)``；默认 None。

    Returns:
        ICAnalysisResult: 按预测窗口组织 IC 时序的分析结果。

    Raises:
        ValueError: 交易日总数不足（少于 ``max(forward_days_list) + 20`` 个）
            时。
    """
    all_dates = get_all_trading_dates(quote_dfs, start_date, end_date)
    max_fwd = max(forward_days_list)
    if len(all_dates) < max_fwd + 20:
        raise ValueError(f'数据不足，至少需要 {max_fwd + 20} 个交易日')

    n = len(all_dates)
    # 每 sample_freq 个交易日取一个截面；i + max_fwd < n 保证该截面的
    # 未来收益窗口完整落在数据范围内，避免尾部截面缺未来数据
    analysis_dates = [
        all_dates[i]
        for i in range(0, n, sample_freq)
        if i + max_fwd < n
    ]

    ic_by_fwd = {fwd: [] for fwd in forward_days_list}
    date_index = []
    total = len(analysis_dates)

    for i, current_date in enumerate(analysis_dates):
        if verbose_cb:
            verbose_cb(i, total, current_date)

        factor_vals = compute_cross_section(
            quote_dfs, flow_dfs, current_date, FACTOR_DEFS)

        for fwd in forward_days_list:
            fwd_returns = compute_forward_returns(quote_dfs, current_date, fwd)
            ic_row = compute_ic_row(factor_vals, fwd_returns, min_stocks)
            ic_by_fwd[fwd].append(ic_row)

        date_index.append(current_date)

    ic_dfs = {
        fwd: pd.DataFrame(ic_by_fwd[fwd], index=date_index)
        for fwd in forward_days_list
    }
    return ICAnalysisResult(ic_dfs, list(forward_days_list))


# ── 结果对象 ──────────────────────────────────────────────────────────────────

class ICAnalysisResult:
    """IC 分析结果容器：按预测窗口组织 IC 时序，并提供汇总/保存/绘图。

    Attributes:
        ic_dfs (dict[int, pd.DataFrame]): 预测窗口 → DataFrame，行 = 截面
            日期、列 = 因子标签、值为该截面该因子的 Spearman IC。
        forward_days_list (list[int]): 参与分析的预测窗口列表。
    """

    def __init__(self, ic_dfs, forward_days_list):
        self.ic_dfs = ic_dfs                    # {fwd: DataFrame(date × factor)}
        self.forward_days_list = forward_days_list

    def summary(self):
        """生成各因子 × 各预测窗口的 IC 汇总统计表。

        返回表 index 为因子标签，列为各预测窗口的指标，列名形如 ``5d_IC_mean``
        / ``5d_IC_std`` / ``5d_IC_IR`` / ``5d_t_stat`` / ``5d_pos_ratio`` /
        ``5d_n``。指标含义：``IC_mean`` 为 IC 均值（方向与平均预测力）；
        ``IC_std`` 为 IC 标准差（稳定性）；``IC_IR`` = IC_mean / IC_std（经波动
        调整后的信息比）；``t_stat`` 为 IC 序列对 0 的单样本 t 检验统计量
        （显著性）；``pos_ratio`` 为 IC 大于 0 的截面占比（方向一致性）；``n``
        为有效截面数。某窗口有效截面数少于 5 个时该窗口各指标全部置 NaN（无
        统计意义）。

        Returns:
            pd.DataFrame: index 为因子标签、列为各预测窗口 IC 指标的汇总表。
        """
        labels = list(self.ic_dfs[self.forward_days_list[0]].columns)
        rows = []
        for lbl in labels:
            row = {'factor': lbl}
            for fwd in self.forward_days_list:
                ic_s = self.ic_dfs[fwd][lbl].dropna()
                prefix = f'{fwd}d_'
                if len(ic_s) < 5:
                    # 截面数过少，指标无统计意义，全部置 NaN
                    row[prefix + 'IC_mean']   = np.nan
                    row[prefix + 'IC_std']    = np.nan
                    row[prefix + 'IC_IR']     = np.nan
                    row[prefix + 't_stat']    = np.nan
                    row[prefix + 'pos_ratio'] = np.nan
                    row[prefix + 'n']         = int(len(ic_s))
                    continue
                ic_mean = ic_s.mean()
                ic_std  = ic_s.std()
                ic_ir   = ic_mean / ic_std if ic_std > 0 else np.nan
                t_stat, _ = ttest_1samp(ic_s, 0)  # H0: IC 均值为 0
                row[prefix + 'IC_mean']   = round(ic_mean, 4)
                row[prefix + 'IC_std']    = round(ic_std, 4)
                row[prefix + 'IC_IR']     = round(ic_ir, 4)
                row[prefix + 't_stat']    = round(t_stat, 4)
                row[prefix + 'pos_ratio'] = round(float((ic_s > 0).mean()), 4)
                row[prefix + 'n']         = int(len(ic_s))
            rows.append(row)
        return pd.DataFrame(rows).set_index('factor')

    def save(self, output_dir):
        """保存 IC 分析结果到输出目录。

        按预测窗口写出 IC 时序 CSV（``ic_ts_{fwd}d.csv``）与汇总统计
        （``ic_summary.csv``），数值均保留 4 位小数；目录不存在时自动创建。

        Args:
            output_dir (str | Path): 输出目录。

        Returns:
            Path: 汇总统计文件 ``ic_summary.csv`` 的路径。
        """
        from pathlib import Path
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        for fwd, df in self.ic_dfs.items():
            df.to_csv(out / f'ic_ts_{fwd}d.csv', float_format='%.4f')

        summary = self.summary()
        path = out / 'ic_summary.csv'
        summary.to_csv(path, float_format='%.4f')
        return path

    def plot(self, output_dir):
        """绘制 IC 分析图表并保存到输出目录。

        生成三张图：``ic_ir_bar.png`` 为各因子 IC_IR 柱状图（按预测窗口分行，
        含 IR = ±0.5 / ±1.0 参考线）；``ic_heatmap.png`` 为 IC 均值热力图
        （因子 × 预测窗口）；``ic_ts_top.png`` 为主预测窗口 IC_IR 绝对值排名
        Top-8 因子的累积 IC 时序 + Top-4 滚动均值（窗口 20、最少 5 期；无 Top
        因子时不生成该图）。全部以 150 dpi 保存。

        Args:
            output_dir (str | Path): 输出目录；不存在时自动创建。

        Returns:
            Path: 柱状图文件 ``ic_ir_bar.png`` 的路径。
        """
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from pathlib import Path

        out = Path(output_dir)
        summary = self.summary()
        factors = summary.index.tolist()
        fwd_list = self.forward_days_list
        n_fac = len(factors)

        # ── 图1: IC_IR 柱状图 ────────────────────────────────────────────────
        fig, axes = plt.subplots(
            len(fwd_list), 1,
            figsize=(max(12, n_fac * 0.5), 4 * len(fwd_list)),
            squeeze=False,
        )
        for ax, fwd in zip(axes[:, 0], fwd_list):
            ir = summary[f'{fwd}d_IC_IR'].fillna(0)
            colors = ['#c0392b' if v < 0 else '#27ae60' for v in ir]
            ax.bar(range(n_fac), ir, color=colors, alpha=0.82, width=0.72)
            ax.axhline(0,     color='black',  lw=0.8)
            ax.axhline( 0.5,  color='orange', lw=1.0, ls='--', alpha=0.8, label='IR=±0.5')
            ax.axhline(-0.5,  color='orange', lw=1.0, ls='--', alpha=0.8)
            ax.axhline( 1.0,  color='red',    lw=1.0, ls='--', alpha=0.8, label='IR=±1.0')
            ax.axhline(-1.0,  color='red',    lw=1.0, ls='--', alpha=0.8)
            ax.set_xticks(range(n_fac))
            ax.set_xticklabels(factors, rotation=45, ha='right', fontsize=7)
            ax.set_title(f'IC_IR  (forward={fwd}d)', fontsize=10)
            ax.set_ylabel('IC_IR')
            ax.legend(fontsize=8, loc='upper right')
        plt.tight_layout()
        p1 = out / 'ic_ir_bar.png'
        fig.savefig(p1, dpi=150, bbox_inches='tight')
        plt.close(fig)

        # ── 图2: IC 均值热力图 ───────────────────────────────────────────────
        hm = pd.DataFrame(
            {f'{f}d': summary[f'{f}d_IC_mean'] for f in fwd_list},
            index=factors,
        )
        vmax = max(0.06, float(hm.abs().max().max()))
        fig2, ax2 = plt.subplots(
            figsize=(max(5, len(fwd_list) * 2),
                     max(6, n_fac * 0.38)),
        )
        im = ax2.imshow(hm.values, aspect='auto',
                        cmap='RdYlGn', vmin=-vmax, vmax=vmax)
        ax2.set_xticks(range(len(fwd_list)))
        ax2.set_xticklabels([f'{f}d' for f in fwd_list], fontsize=9)
        ax2.set_yticks(range(n_fac))
        ax2.set_yticklabels(factors, fontsize=7)
        for r in range(n_fac):
            for c_idx in range(len(fwd_list)):
                v = hm.iloc[r, c_idx]
                if np.isfinite(v):
                    ax2.text(c_idx, r, f'{v:.3f}',
                             ha='center', va='center', fontsize=6.5)
        plt.colorbar(im, ax=ax2, label='IC Mean')
        ax2.set_title('Factor IC Mean Heatmap', fontsize=11)
        plt.tight_layout()
        p2 = out / 'ic_heatmap.png'
        fig2.savefig(p2, dpi=150, bbox_inches='tight')
        plt.close(fig2)

        # ── 图3: Top-8 因子累积 IC + 滚动均值 ───────────────────────────────
        primary_fwd = fwd_list[0]
        ir_series = summary[f'{primary_fwd}d_IC_IR'].dropna().abs()
        top8 = ir_series.nlargest(8).index.tolist()

        if top8:
            ic_ts = self.ic_dfs[primary_fwd][top8].fillna(0)
            cum_ic = ic_ts.cumsum()

            fig3, (ax3a, ax3b) = plt.subplots(2, 1, figsize=(14, 9))

            for col in top8:
                ax3a.plot(cum_ic.index, cum_ic[col], label=col, lw=1.3)
            ax3a.axhline(0, color='black', lw=0.5)
            ax3a.set_title(
                f'Top-8 Factors — Cumulative IC (forward={primary_fwd}d)', fontsize=10)
            ax3a.legend(fontsize=7, ncol=2)
            ax3a.set_ylabel('Cumulative IC')
            ax3a.tick_params(axis='x', rotation=30)

            for col in top8[:4]:
                rolling = ic_ts[col].rolling(20, min_periods=5).mean()
                ax3b.plot(rolling.index, rolling, label=col, lw=1.3)
            ax3b.axhline(0, color='black', lw=0.5)
            ax3b.axhline( 0.04, color='gray', lw=0.8, ls='--', alpha=0.6)
            ax3b.axhline(-0.04, color='gray', lw=0.8, ls='--', alpha=0.6)
            ax3b.set_title(
                f'Top-4 Factors — IC Rolling Mean (window=20, forward={primary_fwd}d)',
                fontsize=10)
            ax3b.legend(fontsize=7)
            ax3b.set_ylabel('IC Rolling Mean')
            ax3b.tick_params(axis='x', rotation=30)

            plt.tight_layout()
            p3 = out / 'ic_ts_top.png'
            fig3.savefig(p3, dpi=150, bbox_inches='tight')
            plt.close(fig3)

        return p1
