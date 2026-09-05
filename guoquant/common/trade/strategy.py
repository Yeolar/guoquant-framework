"""
实时交易选股 / 风控逻辑模块（纯计算，无 IO、无行情依赖）。

选股与止损口径与回测（``guoquant.common.backtest.strategy``）一致：

- 持有打分最高的 top_n 只股票，等权分配；
- 行业分散：同行业最多 ``floor(top_n × 0.4)`` 只（至少 1 只）；
- 时间衰减追踪止损：持有越久止损线越宽（见 ``StopLossManager``）。

被 ``commands/auto_trade.py`` （调仓）与 ``commands/watch.py`` （看盘止损）
引用；本模块函数只做计算，得分、最新价等输入均由调用方传入（得分来自
``trade/scorer.compute_scores``、最新价来自 ``trade/executor.fetch_current_prices``）。
"""
import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 选股                                                                     #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

def select_top_stocks(scores, top_n, industry_map=None):
    """
    从得分中选择 top_n 只股票，做行业分散。

    按得分降序依次选取高分者；提供 ``industry_map`` 时，同一行业入选数量
    不超过 ``max(1, int(top_n × 0.4))`` 只（如 top_n=10 → 最多 4 只），
    无行业映射的股票归入 ``__UNKNOWN__`` 桶、与其他行业同等限流。

    Args:
        scores (dict): ``{code: float}`` 得分 dict。
        top_n (int): 持仓数量上限。
        industry_map (dict, optional): ``{code: industry_code}`` 行业分类；
            缺省时不做行业分散、直接取得分前 top_n 只。

    Returns:
        list: 按得分降序排列的股票代码列表（长度不超过 top_n）。
    """
    if not scores:
        return []

    ranked = sorted(scores, key=scores.get, reverse=True)

    if not industry_map:
        return ranked[:top_n]

    # 行业分散：同行业最多 floor(top_n×0.4) 只（至少 1 只），如 top_n=10 → 4 只
    max_per_industry = max(1, int(top_n * 0.4))

    top_stocks = []
    industry_counts = {}
    for code in ranked:
        # 无行业映射的股票归入 '__UNKNOWN__' 桶，与其他行业同等限流
        ind = industry_map.get(code, '__UNKNOWN__')
        cnt = industry_counts.get(ind, 0)
        if cnt >= max_per_industry:
            continue
        top_stocks.append(code)
        industry_counts[ind] = cnt + 1
        if len(top_stocks) >= top_n:
            break

    return top_stocks


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 止损                                                                     #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

class StopLossManager:
    """
    时间衰减追踪止损管理器，与回测 ``PortfolioRotationStrategy`` 中的逻辑
    一致。

    按持有天数分档，持有越久止损线越宽松（让利润奔跑）：

    - 持有 1-3 天：止损价 = 买入均价 × 0.95（-5%）——买入后立刻下跌是陷阱
      信号；
    - 持有 4-7 天：止损价 = 买入均价 × 0.93（-7%）——震荡期正常容忍；
    - 持有 8 天及以上：止损价 = max(买入均价 × 0.92, 持仓最高价 × 0.90)
      ——既守住成本 -8% 底线，又跟随高点回撤 10% 止盈。

    Attributes:
        stop_loss_pct (float): 兼容保留字段（仅由构造参数写入），实际止损
            使用上述时间衰减分档。
        _entries (dict): ``code`` → 含 ``avg_price`` / ``buy_date`` / ``size``
            的买入持仓记录 dict。
        _high_prices (dict): ``code`` → 持仓期间最高价 float。
    """

    def __init__(self, stop_loss_pct=0.08):
        """初始化止损管理器。

        ``stop_loss_pct`` 仅为兼容保留参数，实际止损使用类 docstring 描述的
        时间衰减分档。

        Args:
            stop_loss_pct (float): 兼容保留的止损比例参数，默认 0.08。
        """
        # stop_loss_pct 仅为兼容保留字段，实际止损使用下方时间衰减分档
        self.stop_loss_pct = stop_loss_pct
        self._entries = {}       # code → {'avg_price': float, 'buy_date': date, 'size': int}
        self._high_prices = {}   # code → 持仓期间最高价

    def register_buy(self, code, price, size, buy_date):
        """记录一笔买入。

        已有持仓时按加权平均更新均价与总量、保留首次买入日期；同时用本次
        价格刷新该代码的持仓期间最高价。

        Args:
            code (str): 股票代码。
            price (float): 买入价。
            size (int): 买入股数。
            buy_date (date): 买入日期（仅首次买入时记录）。
        """
        if code in self._entries:
            old = self._entries[code]
            total_size = old['size'] + size
            avg = (old['avg_price'] * old['size'] + price * size) / total_size
            self._entries[code] = {'avg_price': avg, 'buy_date': old['buy_date'],
                                   'size': total_size}
        else:
            self._entries[code] = {'avg_price': price, 'buy_date': buy_date,
                                   'size': size}
        self._high_prices[code] = max(
            self._high_prices.get(code, price), price)

    def register_sell(self, code):
        """清仓：删除该代码的买入记录与持仓期间最高价。

        Args:
            code (str): 股票代码。
        """
        self._entries.pop(code, None)
        self._high_prices.pop(code, None)

    def update_high(self, code, price):
        """更新持仓期间最高价（仅对已记录的持仓生效）。

        Args:
            code (str): 股票代码。
            price (float): 当前观察到的价格。
        """
        if code in self._high_prices:
            self._high_prices[code] = max(self._high_prices[code], price)

    def check_stop_loss(self, code, current_price, current_date):
        """
        检查是否触发止损。

        依据买入均价、买入日期与持仓期间最高价，按持有天数分档计算止损价
        （档位见类 docstring）；当前价低于止损价即判定触发。

        Args:
            code (str): 股票代码。
            current_price (float): 当前最新价。
            current_date (date): 当前日期（用于计算持有天数；早于买入日期时
                按 0 天计）。

        Returns:
            bool: 触发止损返回 True；未触发、无持仓或 size <= 0 时返回 False。
        """
        entry = self._entries.get(code)
        if not entry or entry['size'] <= 0:
            return False

        avg_price = entry['avg_price']
        buy_date = entry['buy_date']
        highest = self._high_prices.get(code, avg_price)

        # 时间衰减止损：持有越久，止损线越宽松（让利润奔跑）。
        # 档位与理由见类 docstring；8+ 天档取 均价×0.92 与 最高价×0.90 较高者
        # （既守住成本 -8% 底线，又跟随高点回撤 10% 止盈）。
        holding_days = (current_date - buy_date).days if current_date >= buy_date else 0

        if holding_days <= 3:
            stop_price = avg_price * 0.95
        elif holding_days <= 7:
            stop_price = avg_price * 0.93
        else:
            stop_price = max(avg_price * 0.92, highest * 0.90)

        return current_price < stop_price

    def get_stop_price(self, code, current_date):
        """获取当前止损价（供参考展示，不做触发判断）。

        Args:
            code (str): 股票代码。
            current_date (date): 当前日期（用于计算持有天数）。

        Returns:
            float | None: 当前止损价；无该代码持仓记录时返回 None。
        """
        entry = self._entries.get(code)
        if not entry:
            return None

        avg_price = entry['avg_price']
        buy_date = entry['buy_date']
        highest = self._high_prices.get(code, avg_price)
        holding_days = (current_date - buy_date).days if current_date >= buy_date else 0

        if holding_days <= 3:
            return avg_price * 0.95
        elif holding_days <= 7:
            return avg_price * 0.93
        else:
            return max(avg_price * 0.92, highest * 0.90)

    def to_dict(self):
        """序列化为可 JSON 存储的状态 dict。

        Returns:
            dict: 含 ``entries`` 与 ``high_prices`` 两个键——``entries`` 为
            code → 含 ``avg_price`` / ``buy_date`` （isoformat 字符串）/
            ``size`` 的 dict，``high_prices`` 为 code → float。
        """
        entries = {}
        for code, e in self._entries.items():
            entries[code] = {
                'avg_price': e['avg_price'],
                'buy_date': e['buy_date'].isoformat(),
                'size': e['size'],
            }
        return {
            'entries': entries,
            'high_prices': dict(self._high_prices),
        }

    @classmethod
    def from_dict(cls, data):
        """从状态 dict 反序列化，恢复 StopLossManager 实例。

        Args:
            data (dict): ``to_dict`` 产出的状态 dict（键 ``entries`` /
                ``high_prices``，均可缺省；``buy_date`` 为 isoformat 字符串）。

        Returns:
            StopLossManager: 恢复后的实例。
        """
        mgr = cls()
        for code, e in data.get('entries', {}).items():
            mgr._entries[code] = {
                'avg_price': e['avg_price'],
                'buy_date': date.fromisoformat(e['buy_date']),
                'size': e['size'],
            }
        mgr._high_prices = dict(data.get('high_prices', {}))
        return mgr

    def save(self, path):
        """保存止损状态到 JSON 文件（自动创建父目录）。

        Args:
            path (str | Path): 输出文件路径。
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path):
        """从文件加载止损状态；文件不存在时返回初始空管理器。

        Args:
            path (str | Path): 状态文件路径。

        Returns:
            StopLossManager: 恢复的实例；文件不存在时为初始空实例。
        """
        path = Path(path)
        if not path.exists():
            return cls()
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls.from_dict(data)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 调仓计划                                                                 #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

def compute_trade_plan(current_positions, target_codes, stop_loss_manager,
                       current_date, latest_prices, total_cash):
    """
    对比当前持仓与目标持仓，计算调仓订单。

    调仓口径：

    - 止损判定：最新价可得的持仓若触发止损则全量卖出（reason 为
      ``'stop_loss'``）；
    - 卖出：不在目标池中的持仓（reason 为 ``'rebalance'``），与触发止损的
      持仓合并卖出；
    - 买入：目标池中尚未持有、或已因止损卖出的股票，等权分配
      ``total_cash / 目标只数`` 的资金，按最新价折算股数并向下取整到一手
      （100 股）整数倍；
    - 已持有的目标股按等权目标仓位核对：偏离超过 10% 才调仓，超配卖出超出
      部分（留 10% 缓冲）、低配买入补到目标的 90%，同样按一手取整，避免
      频繁交易。

    Args:
        current_positions (dict): ``{code: {'volume': int, 'avg_price': float}}``
            当前持仓。
        target_codes (list): 目标持仓股票代码列表。
        stop_loss_manager (StopLossManager): 止损管理器实例。
        current_date (date): 当前日期（止损持有天数计算用）。
        latest_prices (dict): ``{code: float}`` 最新价；无最新价的股票不参与
            止损与调仓。
        total_cash (float): 账户总资产（用于等权资金分配）。

    Returns:
        dict: 含三个键的订单计划——``'sell'`` 为
        ``[(code, volume, reason), ...]`` （reason 为 ``'stop_loss'`` 或
        ``'rebalance'``）；``'buy'`` 为 ``[(code, volume, target_pct), ...]``；
        ``'stop_loss_codes'`` 为触发止损的代码列表。
    """
    sell_orders = []
    buy_orders = []
    stop_loss_codes = []

    current_set = set(current_positions.keys())
    target_set = set(target_codes)

    # 1. 检查止损
    for code in list(current_set):
        if code in latest_prices:
            if stop_loss_manager.check_stop_loss(code, latest_prices[code], current_date):
                stop_loss_codes.append(code)

    # 2. 卖出不在目标池的 + 触发止损的
    to_sell = (current_set - target_set) | set(stop_loss_codes)
    for code in to_sell:
        if code in current_positions:
            sell_orders.append((code, current_positions[code]['volume'], 'stop_loss' if code in stop_loss_codes else 'rebalance'))

    # 3. 计算买入
    to_buy = target_set - (current_set - set(stop_loss_codes))
    if to_buy:
        # 等权分配：每只目标股分配 total_cash × (1/目标数) 的资金
        target_pct = 1.0 / len(target_codes) if target_codes else 0
        for code in to_buy:
            if code in latest_prices:
                target_value = total_cash * target_pct
                # A 股必须按 100 股（一手）整数倍下单，向下取整
                volume = int(target_value / latest_prices[code] / 100) * 100
                if volume > 0:
                    buy_orders.append((code, volume, target_pct))

    # 4. 对已在持仓中的目标股，按等权调仓（若偏离超过 10%）
    # 注：集合差 - 优先级高于 &，即 current_set & (target_set - 止损集合)
    #     （触发止损者已全量卖出，不再纳入继续持有的范围）
    hold_set = current_set & target_set - set(stop_loss_codes)
    for code in hold_set:
        target_pct = 1.0 / len(target_codes) if target_codes else 0
        if code in current_positions and code in latest_prices:
            current_value = current_positions[code]['volume'] * latest_prices[code]
            target_value = total_cash * target_pct
            diff_pct = (current_value - target_value) / target_value if target_value > 0 else 0
            if abs(diff_pct) > 0.1:  # 偏离目标仓位超 10% 才调仓（避免频繁交易）
                if diff_pct > 0:  # 超配，卖出超出部分（留 10% 缓冲）
                    excess_value = current_value - target_value * 1.1
                    sell_vol = int(excess_value / latest_prices[code] / 100) * 100
                    if sell_vol > 0:
                        sell_orders.append((code, sell_vol, 'rebalance'))
                else:  # 低配，买入不足部分（补到目标的 90%）
                    deficit_value = target_value * 0.9 - current_value
                    buy_vol = int(deficit_value / latest_prices[code] / 100) * 100
                    if buy_vol > 0:
                        buy_orders.append((code, buy_vol, target_pct))

    return {
        'sell': sell_orders,
        'buy': buy_orders,
        'stop_loss_codes': stop_loss_codes,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 人工选股文件读取                                                         #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

def load_selected_codes(filepath, scores=None):
    """
    从文件读取人工选定的股票代码。

    文件内容支持两种格式：

    - 每行一个代码：忽略空行与 ``#`` 开头的注释行；
    - JSON 数组：内容以 ``[`` 开头时按此解析。

    提供 ``scores`` 时按得分降序排序（不在 ``scores`` 中的代码按 0 分处理）。

    Args:
        filepath (str | Path): 选股文件路径。
        scores (dict, optional): ``{code: float}`` 得分 dict，用于排序；
            默认 None（保持文件内顺序）。

    Returns:
        list: 股票代码列表。

    Raises:
        FileNotFoundError: 文件不存在时。
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f'选股文件不存在: {filepath}')

    content = filepath.read_text(encoding='utf-8').strip()

    if content.startswith('['):
        # JSON 格式
        codes = json.loads(content)
    else:
        # 每行一个代码
        codes = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            codes.append(line)

    if scores:
        codes = sorted(codes, key=lambda c: scores.get(c, 0), reverse=True)

    return codes


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 看盘止损                                                                 #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

def compute_watch_plan(positions, stop_loss_manager, current_date, latest_prices):
    """
    看盘模式：仅检查止损，计算需要卖出的持仓。

    遍历全部持仓（volume > 0 且能取到最新价者）：触发止损的进入卖出列表与
    ``stop_loss_codes``；其余进入持有列表并附带市值 / 止损价等展示信息。

    Args:
        positions (dict): ``{code: {'volume': int, 'avg_price': float}}`` 当前
            持仓。
        stop_loss_manager (StopLossManager): 止损管理器实例。
        current_date (date): 当前日期（持有天数计算用）。
        latest_prices (dict): ``{code: float}`` 最新价。

    Returns:
        dict: 含四个键——``'sell'`` 为 ``[(code, volume), ...]``；
        ``'stop_loss_codes'`` 为触发止损的代码列表；``'hold'`` 为继续持有的
        代码列表；``'hold_details'`` 为 code → 含 ``price`` / ``mv`` （市值 =
        volume × 最新价）/ ``stop_price`` 的 dict。
    """
    sell_orders = []
    stop_loss_codes = []
    hold_codes = []
    hold_details = {}

    for code, pos in positions.items():
        if pos['volume'] <= 0:
            continue
        price = latest_prices.get(code)
        if price is None:
            continue

        if stop_loss_manager.check_stop_loss(code, price, current_date):
            stop_loss_codes.append(code)
            sell_orders.append((code, pos['volume']))
        else:
            hold_codes.append(code)
            hold_details[code] = {
                'price': price,
                'mv': pos['volume'] * price,
                'stop_price': stop_loss_manager.get_stop_price(code, current_date),
            }

    return {
        'sell': sell_orders,
        'stop_loss_codes': stop_loss_codes,
        'hold': hold_codes,
        'hold_details': hold_details,
    }
