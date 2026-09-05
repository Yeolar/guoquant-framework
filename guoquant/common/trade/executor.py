"""
QMT 实盘交易执行模块。

通过 ``xtquant`` （``XtQuantTrader`` / ``xtdata``）连接本机 QMT 客户端：查询
账户 / 持仓、执行买卖订单，并提供实时取价（``fetch_current_prices``）；被
``commands/auto_trade.py`` （调仓下单）与 ``commands/watch.py`` （看盘止损）
引用。

``xtquant`` 为可选依赖（随 QMT 客户端分发、不在 pip 上），导入失败时本模块
仍可导入（相关符号为 None），实际调用交易 / 行情接口时才报错。

代码格式转换在边界完成：``execute_orders`` / ``fetch_current_prices`` 接收
内部 Futu 格式代码（``'SZ.000001'``），经 ``context.to_qmt_code`` 转为 QMT
格式（``'000001.SZ'``）后调用；``place_buy`` / ``place_sell`` 等单笔接口
直接收 QMT 格式代码。
"""
import time
import logging
from typing import Optional, Dict, List, Callable

# guoziapp 模板改造：xtquant 为可选依赖（随 QMT 客户端分发，不在 pip 上），
# 故在此容错导入——未安装时本模块仍可导入（相关符号为 None），
# 实际调用交易 / 行情接口时才报错。
try:
    from xtquant import xtdata
    from xtquant.xttrader import XtQuantTrader
    from xtquant.xttype import StockAccount
    from xtquant import xtconstant
except ImportError:
    xtdata = None  # type: ignore
    XtQuantTrader = None  # type: ignore
    StockAccount = None  # type: ignore
    xtconstant = None  # type: ignore

from guoquant.common.fetch.qmtapi.context import to_qmt_code, open_quote_context

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 回调                                                                     #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

class TradingCallback:
    """
    QMT 交易回调处理器，接收账户状态与订单回报。

    回调实例在构造 ``XtQuantTrader`` 时传入（``callback=``），所有方法由 QMT
    推送线程调用，因此只做“写缓存”操作、不做耗时处理，供主线程事后读取。

    Attributes:
        orders (dict): order_id → 订单 dict（含订单状态 / 已成交量等字段）。
        positions (dict): QMT 代码 → 持仓 dict（volume / can_use_volume /
            open_price / market_value）。
        asset (dict | None): 最近一次推送的账户资产 dict；尚未收到推送时为
            None。
        connection_status (bool): 最近一次账户状态推送是否正常（推送字段
            ``status.status == 0`` 时视为正常）。
    """

    def __init__(self):
        self.orders = {}          # order_id → order dict
        self.positions = {}       # code → position dict
        self.asset = None         # 账户资产 dict
        self.connection_status = False

    def on_disconnected(self):
        """连接断开回调：把连接状态置为 False。"""
        self.connection_status = False

    def on_stock_asset(self, asset):
        """账户资产变动回调：缓存最新资产快照。

        Args:
            asset (object): QMT 推送的账户资产对象（含 account_id / cash /
                frozen_cash / market_value / total_asset 字段）。
        """
        self.asset = {
            'account_id': asset.account_id,
            'cash': asset.cash,
            'frozen_cash': asset.frozen_cash,
            'market_value': asset.market_value,
            'total_asset': asset.total_asset,
        }

    def on_stock_order(self, order):
        """订单状态变动回调：按 order_id 缓存订单快照。

        Args:
            order (object): QMT 推送的订单对象（含 order_id / stock_code /
                order_type / price / order_volume / traded_volume /
                order_status / status_msg 字段）。
        """
        self.orders[order.order_id] = {
            'order_id': order.order_id,
            'code': order.stock_code,
            'order_type': order.order_type,
            'price': order.price,
            'volume': order.order_volume,
            'traded_volume': order.traded_volume,
            'status': order.order_status,
            'status_msg': order.status_msg,
        }

    def on_stock_trade(self, trade):
        """成交回报回调：把成交股数与成交价回填到对应订单缓存。

        Args:
            trade (object): QMT 推送的成交对象（含 order_id / traded_volume /
                traded_price 字段）；订单缓存中无该 order_id 时忽略。
        """
        if trade.order_id in self.orders:
            self.orders[trade.order_id]['traded_volume'] = trade.traded_volume
            self.orders[trade.order_id]['traded_price'] = trade.traded_price

    def on_stock_position(self, position):
        """持仓变动回调：按 QMT 代码缓存最新持仓。

        Args:
            position (object): QMT 推送的持仓对象（含 stock_code / volume /
                can_use_volume / open_price / market_value 字段）。
        """
        self.positions[position.stock_code] = {
            'volume': position.volume,
            'can_use_volume': position.can_use_volume,
            'open_price': position.open_price,
            'market_value': position.market_value,
        }

    def on_account_status(self, status):
        """账户状态回调：更新连接状态标记。

        Args:
            status (object): QMT 推送的账户状态对象；``status.status == 0``
                视为连接正常。
        """
        self.connection_status = (status.status == 0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 交易执行器                                                               #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

class QmtExecutor:
    """
    QMT 实盘交易执行器。

    单笔接口（``place_buy`` / ``place_sell`` / ``query_positions`` 等）直接收
    QMT 格式代码（``'000001.SZ'``）；批量调仓统一使用 Futu 格式代码，在
    ``execute_orders`` 内做 Futu → QMT 转换。典型用法：

    .. code-block:: text

        executor = QmtExecutor(mini_path, account_id)
        executor.connect()
        positions = executor.query_positions()
        asset = executor.query_asset()
        executor.place_buy('000001.SZ', 1000, 10.5)
        executor.place_sell('000001.SZ', 1000, 10.6)
        executor.disconnect()

    Attributes:
        mini_path (str): QMT 迷你端路径。
        account_id (str): 资金账号。
        session_id (int): 会话 ID（默认 0）。
        _account (StockAccount): 交易账户对象。
        _trader (XtQuantTrader | None): 交易接口实例；连接成功后非 None。
        _callback (TradingCallback): 回调处理器（缓存账户 / 持仓 / 订单）。
    """

    def __init__(self, mini_path: str, account_id: str, session_id: int = 0):
        """初始化 QMT 实盘交易执行器。

        Args:
            mini_path (str): QMT 迷你端路径（安装目录下 ``userdata_mini``
                所在目录）。
            account_id (str): 资金账号。
            session_id (int): 会话 ID，默认 0。
        """
        self.mini_path = mini_path
        self.account_id = account_id
        self.session_id = session_id

        self._account = StockAccount(account_id)
        self._trader: Optional[XtQuantTrader] = None
        self._callback = TradingCallback()

    def connect(self) -> bool:
        """连接 QMT 交易接口并订阅资金账户。

        构造 ``XtQuantTrader``、启动并连接，成功后订阅账户，随后等待约 1
        秒让回调推送初始数据（资产 / 持仓）。

        Returns:
            bool: 连接并订阅成功返回 True；任一环节失败返回 False。
        """
        self._trader = XtQuantTrader(
            self.mini_path, self.session_id, callback=self._callback)
        self._trader.start()
        result = self._trader.connect()
        if result != 0:
            logger.error(f'连接 QMT 交易接口失败: code={result}')
            return False

        # 订阅账户
        subscribe_result = self._trader.subscribe(self._account)
        if subscribe_result != 0:
            logger.error(f'订阅账户失败: code={subscribe_result}')
            return False

        time.sleep(1)  # 等待回调推送初始数据
        logger.info(f'已连接 QMT 交易接口, 账户: {self.account_id}')
        return True

    def disconnect(self):
        """断开 QMT 交易连接（停止交易线程并释放接口引用）。"""
        if self._trader:
            self._trader.stop()
            self._trader = None

    @property
    def is_connected(self) -> bool:
        """是否已连接且处于正常状态。

        Returns:
            bool: 交易接口已创建，且最近一次账户状态推送正常时返回 True。
        """
        return self._trader is not None and self._callback.connection_status

    def query_asset(self) -> Optional[Dict]:
        """查询账户资产。

        优先走 ``query_stock_asset`` 实时查询，异常时回退到回调缓存
        （``self._callback.asset``）。

        Returns:
            dict | None: 账户资产 dict（含 cash / frozen_cash /
            market_value / total_asset）；未连接时返回 None。
        """
        if not self._trader:
            return None
        try:
            asset = self._trader.query_stock_asset(self._account)
            if asset:
                return {
                    'cash': asset.cash,
                    'frozen_cash': asset.frozen_cash,
                    'market_value': asset.market_value,
                    'total_asset': asset.total_asset,
                }
        except Exception as e:
            logger.error(f'查询资产失败: {e}')
        return self._callback.asset  # fallback 到回调缓存

    def query_positions(self) -> Dict[str, Dict]:
        """
        查询当前持仓。

        优先走 ``query_stock_positions`` 实时查询，异常时回退到回调缓存
        （``self._callback.positions``）。

        Returns:
            dict: QMT 代码 → 持仓 dict（含 volume / can_use_volume /
            open_price / market_value）；未连接时返回空 dict。
        """
        if not self._trader:
            return {}
        try:
            positions = self._trader.query_stock_positions(self._account)
            result = {}
            for pos in positions:
                result[pos.stock_code] = {
                    'volume': pos.volume,
                    'can_use_volume': pos.can_use_volume,
                    'open_price': pos.open_price,
                    'market_value': pos.market_value,
                }
            return result
        except Exception as e:
            logger.error(f'查询持仓失败: {e}')
        return self._callback.positions  # fallback

    def _order(self, stock_code: str, order_type: int, volume: int,
               price_type: int, price: float,
               strategy_name: str = 'auto_trade',
               order_remark: str = '') -> int:
        """内部统一下单方法。

        封装 ``order_stock`` 调用并记录成功 / 失败日志；下单返回负值或抛出
        异常时按失败处理。``strategy_name`` 与 ``order_remark`` 为透传给 QMT
        的下单备注字段。

        Args:
            stock_code (str): QMT 格式代码，如 ``'000001.SZ'``。
            order_type (int): ``xtconstant.STOCK_BUY`` / ``STOCK_SELL``。
            volume (int): 股数。
            price_type (int): 价格类型（如 ``LATEST_PRICE`` 对手价 /
                ``FIX_PRICE`` 限价）。
            price (float): 委托价（``price_type=FIX_PRICE`` 时生效）。
            strategy_name (str): 透传给 QMT 的策略名称，默认 ``'auto_trade'``。
            order_remark (str): 透传给 QMT 的订单备注，默认空字符串。

        Returns:
            int: 下单成功返回订单 ID；未连接、下单失败或异常时返回 -1。
        """
        if not self._trader:
            logger.error('未连接 QMT 交易接口')
            return -1

        try:
            order_id = self._trader.order_stock(
                self._account,
                stock_code,
                order_type,
                volume,
                price_type,
                price,
                strategy_name,
                order_remark,
            )
            if order_id < 0:
                logger.error(f'下单失败: code={order_id} '
                             f'code={stock_code} volume={volume} price={price}')
            else:
                logger.info(f'下单成功: order_id={order_id} '
                            f'code={stock_code} '
                            f'type={"买入" if order_type == xtconstant.STOCK_BUY else "卖出"} '
                            f'volume={volume} price={price}')
            return order_id
        except Exception as e:
            logger.error(f'下单异常: {e}')
            return -1

    def place_buy(self, stock_code: str, volume: int,
                  price: float = 0, price_type: int = xtconstant.LATEST_PRICE):
        """
        买入下单（单笔接口，代码为 QMT 格式）。

        Args:
            stock_code (str): QMT 格式代码，如 ``'000001.SZ'``。
            volume (int): 股数。
            price (float): 限价（``price_type=FIX_PRICE`` 时生效），默认 0。
            price_type (int): 价格类型，默认 ``LATEST_PRICE`` （最新价/对手价）。

        Returns:
            int: 订单 ID；失败返回 -1。
        """
        return self._order(stock_code, xtconstant.STOCK_BUY, volume,
                          price_type, price)

    def place_sell(self, stock_code: str, volume: int,
                   price: float = 0, price_type: int = xtconstant.LATEST_PRICE):
        """
        卖出下单（单笔接口，代码为 QMT 格式）。

        Args:
            stock_code (str): QMT 格式代码，如 ``'000001.SZ'``。
            volume (int): 股数。
            price (float): 限价（``price_type=FIX_PRICE`` 时生效），默认 0。
            price_type (int): 价格类型，默认 ``LATEST_PRICE`` （最新价/对手价）。

        Returns:
            int: 订单 ID；失败返回 -1。
        """
        return self._order(stock_code, xtconstant.STOCK_SELL, volume,
                          price_type, price)

    def execute_orders(self, sell_orders, buy_orders, verbose_cb=None):
        """
        批量执行调仓订单，顺序为先卖后买。

        先卖出以释放资金，再买入；每笔下单间隔约 0.5 秒防止触发风控，全部
        卖出后固定等待约 2 秒让部分成交落地（简化处理，不轮询成交回报）。
        入参代码为内部 Futu 格式，本方法统一经 ``to_qmt_code`` 转为 QMT 格式
        后调用单笔接口。

        Args:
            sell_orders (list): ``[(code, volume, reason), ...]`` 卖出订单，
                code 为 Futu 格式。
            buy_orders (list): ``[(code, volume, target_pct), ...]`` 买入订单，
                code 为 Futu 格式。
            verbose_cb (callable, optional): 进度回调，签名
                ``verbose_cb(stage, info)``；stage 为 ``'sell'`` / ``'buy'``，
                info 为对应订单元组追加 order_id 后的结果。默认 None（不回调）。

        Returns:
            dict: ``{'sold': int, 'bought': int, 'errors': list}``——成功卖出 /
            买入的笔数与失败信息列表（每项为一条失败描述字符串）。
        """
        sold = 0
        bought = 0
        errors = []

        # 先卖后买（释放资金）
        for code, volume, reason in sell_orders:
            qmt_code = to_qmt_code(code)
            order_id = self.place_sell(qmt_code, volume)
            if order_id < 0:
                errors.append(f'卖出失败: {code} {volume}股 ({reason})')
            else:
                sold += 1
            if verbose_cb:
                verbose_cb('sell', (code, volume, reason, order_id))
            time.sleep(0.5)  # 避免下单过快

        # 等待卖出成交（简化处理）
        time.sleep(2)

        for code, volume, target_pct in buy_orders:
            qmt_code = to_qmt_code(code)
            order_id = self.place_buy(qmt_code, volume)
            if order_id < 0:
                errors.append(f'买入失败: {code} {volume}股')
            else:
                bought += 1
            if verbose_cb:
                verbose_cb('buy', (code, volume, target_pct, order_id))
            time.sleep(0.5)

        return {'sold': sold, 'bought': bought, 'errors': errors}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 快捷函数（独立使用）                                                     #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

def fetch_current_prices(codes, period='1d'):
    """
    通过 QMT xtdata 获取最新（当日）收盘价，供实时决策使用。

    与 ``trade/data.fetch_latest_quote`` （完整快照 dict）不同，本函数只取
    收盘价；``xtdata.get_market_data`` 返回“字段名 → DataFrame（行=股票，
    列=时间）”，故取最后一列（最新时间）作为最新价，以
    ``dividend_type='front'`` 前复权口径取价，与历史 K 线口径一致。

    Args:
        codes (list): 股票代码列表（Futu 格式，内部经 ``to_qmt_code`` 转换）。
        period (str): K 线周期，默认 ``'1d'``。

    Returns:
        dict: ``{code: float}`` 最新收盘价，仅包含能取到行情的股票。
    """
    result = {}
    with open_quote_context() as ctx:
        for code in codes:
            qmt_code = to_qmt_code(code)
            try:
                data = xtdata.get_market_data(
                    field_list=[],
                    stock_list=[qmt_code],
                    period=period,
                    count=1,
                    dividend_type='front',
                )
                close_df = data.get('close', None)
                if close_df is not None and not close_df.empty:
                    last_ts = close_df.columns[-1]
                    result[code] = float(close_df.loc[qmt_code, last_ts])
            except Exception:
                pass
    return result
