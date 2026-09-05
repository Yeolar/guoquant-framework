"""Backtrader 调仓/仓位策略模块（原始版）——历史备份，未被引用

本文件是早期版本组合轮换策略的备份，仓库内无任何模块引用它，不参与默认回测
流程。与 ``strategy.py`` （当前主力策略）功能平行，差异在于：本文件保留组合级
风控（单日 / 滚动 5 日亏损减仓）与分数加权仓位，无行业分散与时间衰减追踪
止损。对照/消融实验需手动把 ``runner.py`` 顶部对 ``PortfolioRotationStrategy``
的 import 来源从 ``.strategy`` 改为本模块方可启用。

打分仅用当日及之前数据，调仓订单在次日开盘成交（同 ``strategy.py``），无前视
偏差。
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
    """持仓轮换策略（原始版：分数加权仓位 + 组合级风控，供对照实验）。

    与当前主力 ``strategy.py`` 的等权 + 行业分散 + 时间衰减追踪止损不同，本
    类按分数加权分配仓位（top_n 内分数占比即仓位权重，分数总和小于等于 0 时
    退化为等权兜底），并保留组合级风控与个股固定比例止损。

    组合级风控优先于一切，``risk_mult`` 取各触发条件的最小值：

    - 单日净值跌幅大于 3%：仓位倍数降至 0.5；
    - 滚动 5 日净值跌幅大于 8%：仓位倍数降至 0.3（触发当日生效）。

    触发后进入维持期（``risk_keep_days = 10`` 个交易日），期间仓位不超过
    50%，倒计时归零后恢复正常；未触发风控时，最终仓位倍数取当日
    ``market_regime`` 择时暴露。个股止损：收盘价相对持仓平均成本回撤超过
    ``stop_loss`` 时卖出该持仓（``stop_loss = 0`` 表示禁用）。

    Attributes:
        top_n (int): 单期持仓股票数上限，默认 10。
        scores_by_date (dict): 预计算打分 ``{date: {code: float_score}}``，键
            即换仓日。
        market_regime (dict): 择时仓位暴露 ``{date: float_exposure}``，无数据
            日取 1.0；仅在未触发组合级风控时生效。
        stop_loss (float): 个股止损阈值（收盘价相对平均成本的回撤幅度），0
            表示禁用；默认 0.08。
    """
    params = (
        ('top_n', 10),
        ('scores_by_date', {}),    # {date: {code: float_score}}
        ('market_regime', {}),     # {date: float_exposure}
        ('stop_loss', 0.08),       # 持仓回撤超过此阈值触发止损，0 表示禁用
    )

    def __init__(self):
        # 运行状态：
        # risk_reduced  已触发组合级风控减仓（期间仓位被压低）
        # risk_keep_days 风控维持天数，倒计时归零后恢复正常仓位
        self.trade_log = []        # 成交记录
        self.portfolio_dates = []  # 每日净值日期
        self.portfolio_values = [] # 每日净值
        self.risk_reduced = False   # 是否已触发组合风控减仓
        self.risk_keep_days = 0     # 风控维持天数

    def notify_order(self, order):
        """订单成交回调：记录每笔成交明细到 ``trade_log``。

        仅在订单 Completed 时读取 ``order.executed`` （其余状态无成交数据）。

        Args:
            order (bt.Order): 订单对象，仅 Completed 状态会被记录。
        """
        if order.status == order.Completed:
            action = 'buy' if order.isbuy() else 'sell'
            self.trade_log.append({
                'date': bt.num2date(order.executed.dt).date().isoformat(),
                'code': order.data._name,
                'action': action,
                'price': round(order.executed.price, 4),
                'size': order.executed.size,
                'value': round(order.executed.value, 2),
                'commission': round(order.executed.comm, 4),
                'portfolio_value': round(self.broker.getvalue(), 2),
            })

    def next(self):
        """每个交易日调用一次：记录净值 → 组合风控 → 个股止损 → 换仓。

        风控检查优先于一切：一旦触发减仓，维持期内持续压低仓位，直到维持
        天数耗尽；随后按 ``stop_loss`` 做个股止损。换仓仅发生在
        ``scores_by_date`` 覆盖的日期：先卖出不在目标池的持仓，再对前
        ``top_n`` 只按分数加权分配目标仓位（分数总和小于等于 0 时等权兜底），
        仓位合计不超过择时暴露与风控倍数的乘积。
        """
        current_date = self.datas[0].datetime.date(0)

        # 每日记录净值
        self.portfolio_dates.append(current_date)
        self.portfolio_values.append(self.broker.getvalue())

        # ── 组合级风控检查（优先于一切） ──────────────────────────────
        # risk_mult 表示允许的仓位倍数，多个条件同时触发时取最小值
        risk_mult = 1.0
        if len(self.portfolio_values) >= 2:
            # 单日最大亏损 3%
            daily_rets = [
                self.portfolio_values[i] / self.portfolio_values[i-1] - 1
                for i in range(1, len(self.portfolio_values))
            ]
            if daily_rets and daily_rets[-1] < -0.03:
                risk_mult = min(risk_mult, 0.5)
                self.risk_reduced = True
                self.risk_keep_days = 10

            # 滚动 5 日亏损 8%（第5个交易日前无足够数据，跳过）
            if len(self.portfolio_values) >= 6:
                ret5 = self.portfolio_values[-1] / self.portfolio_values[-6] - 1
                if ret5 < -0.08:
                    risk_mult = min(risk_mult, 0.3)
                    self.risk_reduced = True
                    self.risk_keep_days = 10

        # 风控维持期：减仓状态持续 10 个交易日，期间仓位不超过 50%
        if self.risk_reduced:
            self.risk_keep_days -= 1
            if self.risk_keep_days <= 0:
                self.risk_reduced = False
                risk_mult = 1.0
            else:
                risk_mult = min(risk_mult, 0.5)

        # ── 个股止损检查 ─────────────────────────────────────────────
        # pos.price 为该仓位的平均成本价，收盘价相对成本回撤超阈值即卖出
        if self.p.stop_loss > 0:
            for d in self.datas:
                pos = self.getposition(d)
                if pos.size > 0 and pos.price > 0:
                    drawdown = d.close[0] / pos.price - 1
                    if drawdown < -self.p.stop_loss:
                        self.sell(data=d, size=pos.size)

        if current_date not in self.p.scores_by_date:
            return

        scores = self.p.scores_by_date[current_date]
        if not scores:
            return

        top_stocks = list(
            sorted(scores, key=scores.get, reverse=True)[:self.p.top_n]
        )

        # ── 市场择时信号 ────────────────────────────────────────────
        # 未触发组合风控时，用择时暴露直接决定仓位；已触发则保持风控仓位
        regime_exposure = self.p.market_regime.get(current_date, 1.0)
        if not self.risk_reduced:
            risk_mult = regime_exposure
        final_mult = risk_mult

        # 先卖出不在目标池的持仓（或全部减仓以适应降仓）
        for d in self.datas:
            if d._name not in set(top_stocks):
                self.order_target_percent(data=d, target=0.0)

        # ── 分数加权分配（等权为兜底） ───────────────────────────────
        # 目标仓位 ∝ 分数占比：分数越高权重越大（与 strategy.py 的等权不同）；
        # 分数总和 ≤ 0 时退化为人人等权，保证仓位总和不超过 final_mult
        total_score = sum(scores[s] for s in top_stocks)
        if total_score > 0:
            for d in self.datas:
                if d._name in top_stocks:
                    score_weight = scores[d._name] / total_score
                    target_pct = score_weight * final_mult
                    self.order_target_percent(data=d, target=target_pct)
        else:
            # 兜底：等权
            target_pct = 1.0 / len(top_stocks) * final_mult
            for d in self.datas:
                if d._name in top_stocks:
                    self.order_target_percent(data=d, target=target_pct)
