"""Backtrader 调仓/仓位策略模块

提供回测组合层的调仓/仓位策略：``PortfolioRotationStrategy`` （等权持仓轮换，
含行业分散、时间衰减追踪止损与择时仓位）与 A 股费用模型
``AShareCommission``，被 ``runner.py`` 使用：AShareCommission 作为佣金模型
注册到 broker，PortfolioRotationStrategy 作为回测策略注册到 cerebro。打分由
``scorer.precompute_scores`` 提前算好，本模块只负责按分数调仓、不参与因子
计算。

核心约定：

- 换仓日 = ``scores_by_date`` 的键（由 runner 按 ``rebalance_freq`` 预取），
  策略只在这些日期调仓；
- ``scores_by_date[d]`` 仅使用 d 日及之前的数据计算（见 ``quote.slice_to_date``
  ），策略在 d 日 ``next()`` 发出调仓订单，backtrader 默认在下一根 bar 的开盘
  价撮合，因此“收盘信号 → 次日开盘成交”不会用到当日开盘后信息，无前视偏差。
"""
import backtrader as bt


class AShareCommission(bt.CommInfoBase):
    """A 股佣金模型（backtrader 佣金组件）。

    买入收取佣金（0.01%）；卖出在佣金基础上加收印花税（0.10%），综合费率
    0.11%。印花税仅在卖出时征收，故 ``_getcommission`` 按买卖方向区分费率。

    Attributes:
        commission (float): 佣金费率，默认 0.0001（0.01%）。
        stamp_duty (float): 卖出印花税率，默认 0.001（0.10%）。
    """
    params = (
        ('commission', 0.0001),
        ('stamp_duty', 0.001),
        ('stocklike', True),
        ('commtype', bt.CommInfoBase.COMM_PERC),
    )

    def _getcommission(self, size, price, pseudoexec):
        if size > 0:  # 买入
            return abs(size) * price * self.p.commission
        else:         # 卖出
            return abs(size) * price * (self.p.commission + self.p.stamp_duty)


class PortfolioRotationStrategy(bt.Strategy):
    """等权持仓轮换策略（含行业分散 + 时间衰减追踪止损 + 择时仓位）。

    仅在 ``scores_by_date`` 覆盖的换仓日调仓：按分数降序取前 ``top_n`` 只
    股票等权买入，目标仓位 = 1 / 持仓数 × 当日 ``market_regime`` 仓位倍数
    （订单在次日开盘成交）；其余日期只做止损检查与净值记录。行业分散上，任一
    行业最多占 ``max(1, int(top_n × 0.4))`` 只，防止选股集中于单一行业。

    时间衰减追踪止损在 ``stop_loss > 0`` 时启用，触发价随持有天数放宽：

    - 持有 1-3 天：买入均价 × 0.95（买入后立刻下跌视为陷阱信号）；
    - 持有 4-7 天：买入均价 × 0.93（正常持有期，容忍度放宽）；
    - 持有 8 天及以上：max(买入均价 × 0.92, 持仓最高收盘价 × 0.90)，让利润
      奔跑。

    收盘价低于触发价即卖出该持仓。

    Attributes:
        top_n (int): 单期持仓股票数上限，默认 10。
        scores_by_date (dict): 预计算打分 ``{date: {code: float_score}}``，键
            即换仓日。
        market_regime (dict): 仓位倍数 ``{date: float}``，无数据日取 1.0；由
            runner 合并指数择时（指数偏离 MA60）与横截面波动率暴露得到。
        industry_map (dict): 行业分类 ``{code: industry_code}``，用于行业
            分散。
        stop_loss (float): 时间衰减追踪止损总开关，大于 0 启用、0 禁用；默认
            0.08。
    """
    params = (
        ('top_n', 10),
        ('scores_by_date', {}),
        ('market_regime', {}),
        ('industry_map', {}),
        ('stop_loss', 0.08),
    )

    def __init__(self):
        # 运行时状态（非 backtrader 参数，仅本次回测实例内有效）：
        # _entry_prices:  code → (累计持仓股数, 加权平均买入价)，用于止损判断
        # _entry_dates:   code → 首次买入日期，用于按持有天数分级止损
        # _highest_prices: code → 持仓期间最高收盘价，用于"让利润奔跑"止损
        self.trade_log = []
        self.portfolio_dates = []
        self.portfolio_values = []
        self._entry_prices = {}     # code → (total_size, avg_entry_price)
        self._entry_dates = {}      # code → first_buy_date
        self._highest_prices = {}   # code → 持仓期间最高收盘价

    def notify_order(self, order):
        """订单成交回调：记录成交日志，并维护买入均价/首次买入日期。

        ``order.executed`` 仅在订单 Completed 后有效，因此只处理 Completed
        状态，忽略 pending / rejected / canceled。买入时按加权平均更新
        ``_entry_prices`` 并记录首次买入日期到 ``_entry_dates``，供止损判断
        使用。

        Args:
            order (bt.Order): 订单对象，仅 Completed 状态会被记录。
        """
        if order.status == order.Completed:
            action = 'buy' if order.isbuy() else 'sell'
            code = order.data._name
            price = order.executed.price

            # 更新买入均价（加权）和首次买入日期
            if order.isbuy():
                old_size = self._entry_prices.get(code, (0, 0))
                total_size = old_size[0] + order.executed.size
                avg_price = ((old_size[0] * old_size[1] + order.executed.size * price)
                             / total_size if total_size > 0 else price)
                self._entry_prices[code] = (total_size, avg_price)
                if code not in self._entry_dates:
                    self._entry_dates[code] = order.executed.dt

            self.trade_log.append({
                'date': bt.num2date(order.executed.dt).date().isoformat(),
                'code': code,
                'action': action,
                'price': round(price, 4),
                'size': order.executed.size,
                'value': round(order.executed.value, 2),
                'commission': round(order.executed.comm, 4),
                'portfolio_value': round(self.broker.getvalue(), 2),
            })

    def next(self):
        """每个交易日调用一次：记录净值 → 时间衰减追踪止损 → 换仓。

        记录当日净值后先执行追踪止损（见类 docstring 的分档触发价），随后仅
        当 ``current_date`` 是 ``scores_by_date`` 的键（预计算打分日）时才
        调仓：卖出不在目标池的持仓，再对目标持仓按下单等权 × 择时倍数的目标
        比例买入；非换仓日直接返回。
        """
        current_date = self.datas[0].datetime.date(0)
        self.portfolio_dates.append(current_date)
        self.portfolio_values.append(self.broker.getvalue())

        # ── 时间衰减追踪止损 ───────────────────────────────────────
        # 止损阈值随持有天数放宽：早期严格止损防"买入即套"，后期放宽让利润奔跑
        if self.p.stop_loss > 0:
            for d in self.datas:
                code = d._name
                pos = self.getposition(d)
                if pos.size <= 0:
                    # 已清仓：清除该股票的持仓跟踪状态
                    self._entry_prices.pop(code, None)
                    self._entry_dates.pop(code, None)
                    self._highest_prices.pop(code, None)
                    continue

                cur_close = d.close[0]
                self._highest_prices[code] = max(
                    self._highest_prices.get(code, cur_close), cur_close)

                entry_info = self._entry_prices.get(code)
                entry_dt = self._entry_dates.get(code)
                if entry_info and entry_info[0] > 0 and entry_dt is not None:
                    entry_avg = entry_info[1]
                    highest = self._highest_prices.get(code, entry_avg)
                    holding_days = (current_date - bt.num2date(entry_dt).date()).days

                    if holding_days <= 3:
                        stop_price = entry_avg * 0.95
                    elif holding_days <= 7:
                        stop_price = entry_avg * 0.93
                    else:
                        stop_price = max(entry_avg * 0.92, highest * 0.90)

                    if cur_close < stop_price:
                        self.sell(data=d, size=pos.size)

        # 非换仓日直接返回（止损已在上面执行）
        if current_date not in self.p.scores_by_date:
            return

        scores = self.p.scores_by_date[current_date]
        if not scores:
            return

        # ── 行业分散选股 ───────────────────────────────────────────
        # 按分数降序取前 top_n 只，但任一行业最多占 max_per_industry 只，
        # 防止选股集中于单一行业（max_per_industry = max(1, int(top_n×0.4))）
        ranked = sorted(scores, key=scores.get, reverse=True)
        industry_map = self.p.industry_map
        max_per_industry = max(1, int(self.p.top_n * 0.4))

        top_stocks = []
        industry_counts = {}
        for code in ranked:
            ind = industry_map.get(code, '__UNKNOWN__')
            cnt = industry_counts.get(ind, 0)
            if cnt >= max_per_industry:
                continue
            top_stocks.append(code)
            industry_counts[ind] = cnt + 1
            if len(top_stocks) >= self.p.top_n:
                break

        top_set = set(top_stocks)

        # 卖出不在目标池的持仓（order_target_percent 自动按目标比例调仓，
        # 目标 0.0 即全部卖出）
        for d in self.datas:
            if d._name not in top_set:
                self.order_target_percent(data=d, target=0.0)

        # 等权 × 择时倍数买入
        # regime_mult 来自 runner 的 market_regime（指数择时 × 波动率暴露），
        # 目标仓位 = 1/top_n × regime_mult；订单在次日开盘成交，无前视偏差
        regime_mult = self.p.market_regime.get(current_date, 1.0)
        target_pct = 1.0 / len(top_stocks) * regime_mult if top_stocks else 0
        for d in self.datas:
            if d._name in top_set:
                self.order_target_percent(data=d, target=target_pct)
