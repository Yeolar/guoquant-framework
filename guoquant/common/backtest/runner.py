"""
回测运行器

整合数据加载、预计算打分、backtrader 执行、结果报告的主入口，产出
BacktestResult（统计指标 / 报告 / CSV / 净值图），被
guoquant.commands.backtest（命令行入口）调用。

分层与引用关系：

- 行情 / 资金流 / 筹码整表加载统一走 guoquant.common.quote
  （load_quote_dfs / load_flow_dfs / load_dist_records）；行业映射、指数
  行情与 bt feed 用 .data；
- 打分预计算在 .scorer（precompute_scores 及引擎专用预计算因子）；策略在
  guoquant.strategies 导入即完成注册（@register_strategy，注册名 = 打分
  核心函数全名，见 common/strategy.py），通用打分执行在 common/scoring.py；
- 佣金模型与持仓轮换策略在 .strategy。

回测流程概览：

1. 加载行情 / 资金流 / 筹码 / 行业映射 / 指数数据。
2. 计算市场择时（指数 vs MA60）与横截面波动率暴露。
3. 确定换仓日，预计算各日打分（仅用到当日及之前数据，无前视偏差）。
4. 用 backtrader 模拟：信号在换仓日 next() 发出，次日开盘价成交。
"""
import csv
import math
from datetime import date
from pathlib import Path

import backtrader as bt
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from guoquant.common.quote import (
    load_quote_dfs,
    load_flow_dfs,
    load_dist_records,
)
from .data import (
    load_industry_map,
    load_index_df,
    load_all_industry_index_dfs,
    make_bt_feed,
    get_all_trading_dates,
)
# 引擎入口：导入即完成全部策略注册（strategies/*.py 内的 @register_strategy
# 装饰器在导入时执行，注册表见 guoquant.common.strategy）；needs_flow 等
# 注册信息在下方查找前必须已注册完毕，故在此显式导入
import guoquant.strategies  # noqa: F401

from .scorer import (
    precompute_scores, registered_strategies, get_strategy_names,
    _compute_sector_momentum, _compute_cs_vol_exposure,
)
from .strategy import AShareCommission, PortfolioRotationStrategy


class BacktestResult:
    """一次回测的完整结果：净值曲线、交易日志与统计指标。

    各字段由 BacktestRunner.run() 汇总回填：净值曲线由策略在每个 bar 逐条
    记录，统计指标由本类只读属性实时计算，不依赖 cerebro analyzer 输出。

    Attributes:
        initial_cash (float): 初始资金，构造参数，由调用方指定。
        final_value (float): 回测结束时的账户总值，由 run() 从 broker
            读取后写入。
        portfolio_values (list of float): 逐 bar 账户净值序列，由策略在
            每个 bar 记录，与 dates 等长一一对应。
        dates (list of date): 与 portfolio_values 对应的日期序列，由策略
            记录。
        trade_log (list of dict): 每笔已成交订单的记录，键为 date / code /
            action / price / size / value / commission / portfolio_value
            （即 save_trades 写入的列），由策略在订单成交回调中记录。
        strategy_name (str): 策略名（打分核心函数全名），由调用方指定。
        start_date (date): 回测起始日期（含）。
        end_date (date): 回测结束日期（含）。
        top_n (int): 每期持仓股票只数。
        rebalance_freq (int): 换仓间隔（个交易日）。
    """

    def __init__(self, *, initial_cash, final_value, portfolio_values, dates,
                 trade_log, strategy_name, start_date, end_date, top_n, rebalance_freq):
        """构造回测结果对象。

        参数即本对象的字段，由 BacktestRunner.run() 按回测产出传入；本方法
        只保存、不计算，统计指标由只读属性实时计算。

        Args:
            initial_cash (float): 初始资金。
            final_value (float): 回测结束时的账户总值。
            portfolio_values (list of float): 逐 bar 账户净值序列。
            dates (list of date): 与 portfolio_values 对应的日期序列。
            trade_log (list of dict): 每笔成交记录。
            strategy_name (str): 策略名。
            start_date (date): 回测起始日期（含）。
            end_date (date): 回测结束日期（含）。
            top_n (int): 每期持仓股票只数。
            rebalance_freq (int): 换仓间隔（个交易日）。
        """
        self.initial_cash = initial_cash
        self.final_value = final_value
        self.portfolio_values = portfolio_values   # list[float], 每日
        self.dates = dates                         # list[date]，与 portfolio_values 对应
        self.trade_log = trade_log
        self.strategy_name = strategy_name
        self.start_date = start_date
        self.end_date = end_date
        self.top_n = top_n
        self.rebalance_freq = rebalance_freq

    # ------------------------------------------------------------------ #
    # 统计指标                                                              #
    # ------------------------------------------------------------------ #

    @property
    def total_return(self):
        """总收益率：最终净值相对初始资金的净收益。

        Returns:
            float: ``final_value / initial_cash - 1``，为小数（含佣金与滑点
                损耗后的净收益）。
        """
        return self.final_value / self.initial_cash - 1

    @property
    def cagr(self):
        """年化复合收益率，按首尾日期的实际跨度年化。

        日期不足两个或首尾跨度为 0 时返回 0.0；一年按 365.25 个自然日计。

        Returns:
            float: ``(final_value / initial_cash) ** (1 / n_years) - 1``。
        """
        if not self.dates or len(self.dates) < 2:
            return 0.0
        n_years = (self.dates[-1] - self.dates[0]).days / 365.25
        if n_years <= 0:
            return 0.0
        return (self.final_value / self.initial_cash) ** (1 / n_years) - 1

    @property
    def max_drawdown(self):
        """最大回撤：净值从历史峰值到谷值的最大跌幅。

        Returns:
            float: 峰值到谷值的最小日净值相对峰值收益（负数，越接近 0 表示
                回撤越小）。
        """
        peak = self.portfolio_values[0]
        max_dd = 0.0
        for v in self.portfolio_values:
            if v > peak:
                peak = v
            dd = v / peak - 1
            if dd < max_dd:
                max_dd = dd
        return max_dd

    @property
    def sharpe(self):
        """年化 Sharpe 比率，按逐日净值收益率序列计算。

        无风险利率取 2.5%（日化按 252 个交易日折算），年化乘子为 252 的
        平方根；观测值不足两个或收益率标准差为 0 时返回 0.0。

        Returns:
            float: 年化 Sharpe 比率。
        """
        if len(self.portfolio_values) < 2:
            return 0.0
        import statistics
        daily_rets = [
            self.portfolio_values[i] / self.portfolio_values[i - 1] - 1
            for i in range(1, len(self.portfolio_values))
        ]
        if not daily_rets:
            return 0.0
        mean_r = sum(daily_rets) / len(daily_rets)
        std_r = statistics.stdev(daily_rets) if len(daily_rets) > 1 else 0
        if std_r == 0:
            return 0.0
        rf_daily = 0.025 / 252
        return (mean_r - rf_daily) / std_r * math.sqrt(252)

    @property
    def win_rate(self):
        """胜率：已平仓（卖出）交易的盈利比例；当前未实现。

        缺少逐仓位盈亏追踪，本属性暂不统计胜率：无卖出成交时返回 0.0，
        存在卖出成交时返回 None。建议改用 cerebro 的 analyzer
        （TradeAnalyzer）在回测后统计。

        Returns:
            float or None: 无卖出成交返回 0.0；有卖出成交返回 None（未
                实现）。
        """
        sells = [t for t in self.trade_log if t['action'] == 'sell' and t['size'] != 0]
        if not sells:
            return 0.0
        return None

    # ------------------------------------------------------------------ #
    # 输出                                                                 #
    # ------------------------------------------------------------------ #

    def print_report(self, console=None):
        """在控制台打印一次回测的统计报告。

        内容含策略 / 周期 / 持仓 / 换仓频率配置，以及初始资金、最终净值、
        总收益率、年化收益、最大回撤、Sharpe 与总交易笔数。

        Args:
            console (object, optional): 带 print 方法的输出对象（如 rich 的
                Console）；提供时报告经其输出，否则使用内置 print。
        """
        def _p(s):
            if console:
                console.print(s)
            else:
                print(s)

        _p('')
        _p('─' * 60)
        _p(f'  策略: {self.strategy_name}'
           f'  |  周期: {self.start_date} ~ {self.end_date}')
        _p(f'  持仓: {self.top_n} 只  |  换仓频率: 每 {self.rebalance_freq} 个交易日')
        _p('─' * 60)
        _p(f'  初始资金:  {self.initial_cash:>14,.0f}')
        _p(f'  最终净值:  {self.final_value:>14,.2f}')
        _p(f'  总收益率:  {self.total_return:>+14.2%}')
        _p(f'  年化收益:  {self.cagr:>+14.2%}')
        _p(f'  最大回撤:  {self.max_drawdown:>14.2%}')
        _p(f'  Sharpe:    {self.sharpe:>14.2f}')
        _p(f'  总交易笔数: {len(self.trade_log):>13,}')
        _p('─' * 60)
        _p('')

    def save_trades(self, output_dir):
        """把每一笔成交写入 trades.csv 并返回文件路径。

        目录不存在时自动创建；先写表头再逐条写 trade_log 记录，列为 date /
        code / action / price / size / value / commission / portfolio_value。
        交易日志为空时只创建目录并返回路径，不写文件。

        Args:
            output_dir (Path or str): 输出目录。

        Returns:
            Path: trades.csv 的完整路径。
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / 'trades.csv'
        if not self.trade_log:
            return path

        fieldnames = ['date', 'code', 'action', 'price', 'size',
                      'value', 'commission', 'portfolio_value']
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self.trade_log)
        return path

    def save_plot(self, output_dir):
        """保存归一化净值曲线图到 equity_curve.png 并返回路径。

        上下两段子图：上为归一化净值曲线（标注最大回撤区间，标题含收益 /
        年化 / 回撤 / Sharpe 统计），下为每日回撤面积图；portfolio_values
        或 dates 为空时只创建目录并返回路径，不生成图片。

        Args:
            output_dir (Path or str): 输出目录。

        Returns:
            Path: equity_curve.png 的完整路径。
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / 'equity_curve.png'

        if not self.portfolio_values or not self.dates:
            return path

        import pandas as pd
        s = pd.Series(self.portfolio_values, index=self.dates)
        normalized = s / s.iloc[0]

        fig, axes = plt.subplots(2, 1, figsize=(14, 8),
                                 gridspec_kw={'height_ratios': [3, 1]})

        # 上方：净值曲线
        ax1 = axes[0]
        ax1.plot(normalized.index, normalized.values,
                 color='steelblue', linewidth=1.5,
                 label=f'{self.strategy_name} strategy')

        # 标注最大回撤区间
        peak_idx = 0
        max_dd_end = 0
        peak = normalized.iloc[0]
        trough = normalized.iloc[0]
        max_dd = 0.0
        for i, v in enumerate(normalized):
            if v > peak:
                peak = v
                peak_idx = i
                trough = v
            if v < trough:
                trough = v
                if trough / peak - 1 < max_dd:
                    max_dd = trough / peak - 1
                    max_dd_end = i

        if max_dd < 0:
            ax1.axvspan(normalized.index[peak_idx],
                        normalized.index[max_dd_end],
                        alpha=0.15, color='red', label='Max Drawdown')

        ax1.axhline(y=1.0, color='gray', linestyle='--', linewidth=0.8, alpha=0.5)
        ax1.set_ylabel('Normalized NAV')
        ax1.set_title(
            f'[{self.strategy_name}]  {self.start_date} ~ {self.end_date}  '
            f'Return {self.total_return:+.1%}  '
            f'CAGR {self.cagr:+.1%}  '
            f'MaxDD {self.max_drawdown:.1%}  '
            f'Sharpe {self.sharpe:.2f}',
            fontsize=10
        )
        ax1.legend(loc='upper left')
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%y-%m'))
        ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha='right')

        # 下方：每日回撤
        ax2 = axes[1]
        drawdown_series = normalized / normalized.cummax() - 1
        ax2.fill_between(drawdown_series.index, drawdown_series.values,
                         0, color='red', alpha=0.4)
        ax2.set_ylabel('Drawdown')
        ax2.set_ylim(min(drawdown_series.min() * 1.1, -0.01), 0.01)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%y-%m'))
        ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30, ha='right')

        plt.tight_layout()
        plt.savefig(path, dpi=150, bbox_inches='tight')
        plt.close(fig)
        return path


# ------------------------------------------------------------------ #
# 主运行器                                                             #
# ------------------------------------------------------------------ #

class BacktestRunner:
    """回测主入口：整合数据加载、打分预计算与 backtrader 执行。

    策略名即打分核心函数全名（@register_strategy 注册，见
    guoquant.common.strategy）；内置如 stock_strength_strategy /
    stock_flow_strategy / stock_main_wave_strategy / stock_pullback_strategy
    等，全部已注册名可用 scorer.get_strategy_names() 查询，
    user_strategies.py 可继续扩展。theme_strength_strategy 与
    market_sentiment_cycle_strategy 为命令专用策略（不注册），不经本引擎
    运行。

    用法示例：

    .. code-block:: python

        runner = BacktestRunner(
            data_root='out/260710/fetched_data',
            strategy_name='stock_main_wave_strategy',
            initial_cash=1_000_000,
            top_n=10,
            rebalance_freq=5,
        )
        result = runner.run(start_date=date(2025,1,1), end_date=date(2026,7,10))
        result.print_report()
        result.save_trades('out/260710/backtest')
        result.save_plot('out/260710/backtest')

    Attributes:
        data_root (Path): 数据根目录，构造时由 data_root 参数转换而来
            （调用方指定）。
        strategy_name (str): 策略名（打分核心函数全名），默认
            stock_strength_strategy。
        initial_cash (float): 初始资金，默认 1,000,000。
        top_n (int): 每期持仓股票只数，默认 10。
        rebalance_freq (int): 换仓间隔（个交易日），默认 5。
        stop_loss (float): 透传给 PortfolioRotationStrategy 的追踪止损
            开关参数（>0 启用、0 禁用，默认 0.08；分级止损口径见
            guoquant.common.backtest.strategy）。
    """

    def __init__(self, data_root, strategy_name='stock_strength_strategy',
                 initial_cash=1_000_000, top_n=10, rebalance_freq=5,
                 stop_loss=0.08):
        """初始化回测运行器的固定参数。

        Args:
            data_root (str or Path): 数据根目录（其下应含 quote 等回测所需
                数据子目录）。
            strategy_name (str): 注册的策略名，默认
                stock_strength_strategy。
            initial_cash (float): 初始资金，默认 1,000,000。
            top_n (int): 每期持仓股票只数，默认 10。
            rebalance_freq (int): 换仓间隔（个交易日），默认 5。
            stop_loss (float): 追踪止损开关参数（>0 启用、0 禁用），默认
                0.08。
        """
        self.data_root = Path(data_root)
        self.strategy_name = strategy_name
        self.initial_cash = initial_cash
        self.top_n = top_n
        self.rebalance_freq = rebalance_freq
        self.stop_loss = stop_loss

    def run(self, start_date, end_date, verbose_cb=None):
        """执行一次完整回测并返回结果对象。

        主流程：加载行情与（按策略 needs_flow 注册标记按需加载的）资金流 /
        筹码、行业映射、行业指数及指数行情；按指数偏离 MA60 的幅度分四档
        （1.0 / 0.8 / 0.6 / 0.5）得到市场择时，与横截面波动率暴露（裁剪至
        [0.3, 1.0]）相乘得到各换仓日的组合仓位倍数（取不到信号的日期默认
        1.0）；在区间内每 rebalance_freq 个交易日取一个换仓点并预计算各日
        打分（仅用当日及之前数据，无前视偏差）；把出现在任意换仓日打分中的
        股票加入 cerebro feed，由 PortfolioRotationStrategy 在换仓日
        next() 发出调仓信号、次一交易日开盘价成交；净值曲线由策略逐 bar
        记录，统计指标由返回的 BacktestResult 计算。

        Args:
            start_date (date): 回测起始日期（含）。
            end_date (date): 回测结束日期（含）。
            verbose_cb (callable, optional): 进度回调
                verbose_cb(stage: str, info)，stage 如 loading_quote /
                loading_flow / scoring / adding_feeds / running 等。

        Returns:
            BacktestResult: 回测结果（净值曲线、交易日志与统计指标）。

        Raises:
            ValueError: 未找到行情数据、回测区间内无交易日，或没有可用的
                数据 feed 时抛出。
        """
        # ── 1. 加载数据 ────────────────────────────────────────────────
        if verbose_cb:
            verbose_cb('loading_quote', self.data_root)
        quote_dfs = load_quote_dfs(self.data_root)
        if not quote_dfs:
            raise ValueError(f'未找到行情数据: {self.data_root}/quote/d/')

        # flow 数据按策略的注册标记按需加载（needs_flow，见
        # common/strategy.register_strategy 的引擎入口配置）
        needs_flow = (registered_strategies.get(self.strategy_name, (None, False))[1]
                      if self.strategy_name in registered_strategies else False)

        flow_dfs = {}
        dist_records = {}
        if needs_flow:
            if verbose_cb:
                verbose_cb('loading_flow', self.data_root)
            flow_dfs = load_flow_dfs(self.data_root)
            dist_records = load_dist_records(self.data_root)

        # ── 2. 加载行业映射 ────────────────────────────────────────
        industry_map = load_industry_map(self.data_root)

        # ── 3. 加载行业指数数据 ──────────────────────────────────────
        industry_index_dfs = load_all_industry_index_dfs(self.data_root)

        # ── 4. 加载指数数据，计算市场择时信号 ─────────────────────────
        index_df = load_index_df(self.data_root, 'SH.000300')
        market_regime = {}
        if index_df is not None:
            index_close = index_df['c']
            index_ma60 = index_close.rolling(60).mean()
            for dt, row in index_df.iterrows():
                d = dt.date()
                if pd.isna(index_ma60.loc[dt]):
                    continue
                # 渐进式择时：按当日指数偏离 MA60 的幅度分四档暴露
                # （即下方 1.0 / 0.8 / 0.6 / 0.5）。MA60 只用截至当日的
                # 收盘数据滚动计算，该择时决定换仓日的目标仓位，订单在
                # 次一交易日开盘成交，无前视偏差。
                deviation = row['c'] / index_ma60.loc[dt] - 1
                if deviation >= 0.02:
                    market_regime[d] = 1.0
                elif deviation >= 0:
                    market_regime[d] = 0.8
                elif deviation >= -0.02:
                    market_regime[d] = 0.6
                else:
                    market_regime[d] = 0.5

        # ── 5. 确定换仓日期 ────────────────────────────────────────────
        # 取回测区间内所有交易日，每 rebalance_freq 天取一个换仓点
        all_dates = get_all_trading_dates(quote_dfs, start_date, end_date)
        if not all_dates:
            raise ValueError(f'回测区间 {start_date}~{end_date} 内无交易日')

        rebalance_dates = all_dates[::self.rebalance_freq]

        # ── 6. 预计算行业轮动动量（仅 strength / flow 使用） ──────────
        sector_momentum_by_date = {}
        _use_sector = self.strategy_name in ('stock_strength_strategy', 'stock_flow_strategy')
        if _use_sector and industry_index_dfs:
            for d in rebalance_dates:
                sm = _compute_sector_momentum(
                    quote_dfs, industry_index_dfs, industry_map, d, window=20)
                if sm:
                    sector_momentum_by_date[d] = sm

        # ── 7. 预计算横截面波动率仓位暴露 ─────────────────────────────
        cs_exposure_by_date = _compute_cs_vol_exposure(
            quote_dfs, rebalance_dates, lookback=60)

        # 合并择时信号：market_regime（四档，见上）× cs_vol_exposure
        # （裁剪至 [0.3, 1.0]），相乘得到该换仓日的组合仓位倍数；
        # 取不到信号的日期默认为 1.0，即不做仓位折扣的满仓目标。
        combined_regime = {}
        for d in rebalance_dates:
            mr = market_regime.get(d, 1.0)
            cs = cs_exposure_by_date.get(d, 1.0)
            combined_regime[d] = mr * cs

        # ── 8. 预计算分数 ──────────────────────────────────────────────
        if verbose_cb:
            verbose_cb('scoring', len(rebalance_dates))

        scores_by_date = precompute_scores(
            quote_dfs=quote_dfs,
            rebalance_dates=rebalance_dates,
            strategy_name=self.strategy_name,
            flow_dfs=flow_dfs if needs_flow else None,
            dist_records=dist_records if needs_flow else None,
            verbose_cb=lambda d, n: (verbose_cb('scored', (d, n)) if verbose_cb else None),
            sector_momentum_by_date=sector_momentum_by_date,
        )

        # ── 9. 创建 cerebro ────────────────────────────────────────────
        cerebro = bt.Cerebro(stdstats=False, preload=True, runonce=True)
        cerebro.broker.set_cash(self.initial_cash)
        cerebro.broker.addcommissioninfo(AShareCommission())

        # 观察器 / 分析器（供 cerebro 内部使用；返回结果不经其收集，
        # 净值曲线由策略逐 bar 记录，统计指标由 BacktestResult 计算）
        cerebro.addobserver(bt.observers.Broker)
        cerebro.addobserver(bt.observers.DrawDown)

        cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
        cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
        cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe',
                            riskfreerate=0.025, annualize=True, timeframe=bt.TimeFrame.Days)
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')

        # ── 10. 添加数据 feed ───────────────────────────────────────────
        if verbose_cb:
            verbose_cb('adding_feeds', len(quote_dfs))

        # 只添加出现在任何换仓日分数中的股票（减少无用 feed）
        scored_codes = set()
        for d_scores in scores_by_date.values():
            scored_codes.update(d_scores.keys())

        added = 0
        for code in scored_codes:
            if code not in quote_dfs:
                continue
            feed = make_bt_feed(code, quote_dfs[code], start_date, end_date)
            if feed is not None:
                cerebro.adddata(feed, name=code)
                added += 1

        if added == 0:
            raise ValueError('没有可用的数据 feed，请检查数据路径和日期范围')

        # ── 11. 添加策略 ────────────────────────────────────────────────
        cerebro.addstrategy(
            PortfolioRotationStrategy,
            top_n=self.top_n,
            scores_by_date=scores_by_date,
            market_regime=combined_regime,
            industry_map=industry_map,
            stop_loss=self.stop_loss,
        )

        # ── 12. 运行回测 ────────────────────────────────────────────────
        if verbose_cb:
            verbose_cb('running', added)

        results = cerebro.run()
        strat = results[0]

        # ── 13. 收集净值曲线（由 strategy.next() 直接记录） ────────────────
        portfolio_values = strat.portfolio_values
        obs_dates = strat.portfolio_dates
        final_value = cerebro.broker.getvalue()

        return BacktestResult(
            initial_cash=self.initial_cash,
            final_value=final_value,
            portfolio_values=portfolio_values,
            dates=obs_dates,
            trade_log=strat.trade_log,
            strategy_name=self.strategy_name,
            start_date=start_date,
            end_date=end_date,
            top_n=self.top_n,
            rebalance_freq=self.rebalance_freq,
        )
