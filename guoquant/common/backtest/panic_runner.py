"""
极端恐慌逆向策略 — 独立事件驱动回测引擎

不依赖 backtrader，直接基于行情数据进行回测；被
guoquant.commands.panic_backtest（命令行入口）调用。本模块独立读取
quote/d/stock/ 下的 parquet、自行实现交易日并集，不依赖 backtest/data.py
的数据加载与 backtrader，与 runner.py 的 PortfolioRotationStrategy 回测
流程相互独立，可并行使用。

策略逻辑：

- 每日计算全市场恐慌指标：均收益率、跌幅超标占比、跌停占比、5 日累积跌幅。
- 满足恐慌条件 → 次日开盘等权买入 top_n 只超跌且高流动性的标的。
- 持有 hold_days 个交易日后收盘卖出（含止损）。
- 多个恐慌信号独立追踪，资金在已占用与可用之间流转。

恐慌检测条件（口径见 detect_all_panics）：

- 普跌型：avg_return 低于 -3% 且 decline_5pct_ratio 高于 15%。
- 极端暴跌：avg_return 低于 -5%。
- 连续下跌型：5 日累积收益低于 -6% 且 avg_return 低于 -2%。

标的选择（逆向排序，口径见 select_stocks）：

- Score = 0.5 × norm(5d 跌幅) + 0.3 × norm(20d 成交额) + 0.2 × norm(1 -
  60d 价格位置)，三个维度先各自 min-max 归一化到 [0, 1] 再加权。
- 排除：当日跌停、换手率低于 1%、20 日均成交额低于 500 万。

无前视偏差说明：

恐慌信号基于收盘数据确认（当日全市场统计），买入在次日开盘价执行，
卖出在计划退出日（或止损日）收盘价执行，全程不使用未来信息。
"""

from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 数据加载（直接读取 quote/d/stock/ 下的 parquet）     #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #


def _load_parquet_quote(path: Path) -> Optional[pd.DataFrame]:
    """加载单只股票的 parquet 行情文件，返回以 datetime 为索引的 DataFrame。

    parquet 为抓取器主格式（t 列已为 datetime，K 线列约定见
    guoquant.common.quote）：这里把 t 转 datetime 后设为索引并升序排列；
    单文件损坏时返回 None，由上层跳过，不影响整体加载。

    Args:
        path (Path): parquet 行情文件路径。

    Returns:
        DataFrame or None: 以升序 datetime 为索引的行情表；解析失败时返回
            None。
    """
    try:
        df = pd.read_parquet(path)
        df['datetime'] = pd.to_datetime(df['t'])
        return df.set_index('datetime').sort_index()
    except Exception:
        return None


def load_all_quote_dfs_parquet(data_root: str | Path) -> Dict[str, pd.DataFrame]:
    """从 data_root/quote/d/stock/ 加载全部 parquet 格式行情数据。

    返回 {code: DataFrame}，code 取自文件名（不含扩展名，如 SH.600051）；
    行情位于 quote/d/stock/ 目录（与 index/ 平行，不再按市值分层）。单文件
    损坏或行数不足 20 的视为无效丢弃。

    Args:
        data_root (str or Path): 数据根目录。

    Returns:
        dict of str to DataFrame: code → 以 datetime 为索引的行情表
            （含 t / o / c / h / l / v / a / tr / pe / lc 等列，K 线列约定
            同 guoquant.common.quote 的行情格式）；目录不存在时返回空 dict。
    """
    data_root = Path(data_root)
    quote_dir = data_root / 'quote' / 'd' / 'stock'
    if not quote_dir.exists():
        return {}

    result: Dict[str, pd.DataFrame] = {}
    for path in quote_dir.iterdir():
        if path.suffix == '.parquet':
            code = path.stem  # e.g. 'SH.600051'
            df = _load_parquet_quote(path)
            if df is not None and len(df) >= 20:
                result[code] = df
    return result


def get_all_trading_dates_from_dfs(quote_dfs: Dict[str, pd.DataFrame],
                                    start_date: Optional[date] = None,
                                    end_date: Optional[date] = None) -> List[date]:
    """汇总所有行情 DataFrame 的交易日，返回排序去重后的日期列表（并集）。

    注意：这是并集而非严格共同交易日——某天只要有任意股票有数据即被包含，
    用于确定买入 / 退出日，避免因个股停牌导致整日缺失。日期为 date 对象
    （仅年月日）。

    Args:
        quote_dfs (dict of str to DataFrame): code → 行情 DataFrame。
        start_date (date, optional): 只保留不早于该日期的交易日。
        end_date (date, optional): 只保留不晚于该日期的交易日。

    Returns:
        list of date: 升序、去重的交易日列表。
    """
    all_dates_set = set()
    for df in quote_dfs.values():
        all_dates_set.update(df.index.date)

    all_dates = sorted(all_dates_set)
    if start_date:
        all_dates = [d for d in all_dates if d >= start_date]
    if end_date:
        all_dates = [d for d in all_dates if d <= end_date]
    return all_dates


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 恐慌信号                                                               #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

@dataclass
class PanicSignal:
    """一次市场恐慌事件，由 detect_all_panics 按日识别并构造。

    Attributes:
        date (date): 恐慌确认日（基于当日收盘数据统计）。
        avg_return (float): 当日全市场横截面平均收益率（小数）。
        decline_5pct_ratio (float): 当日日收益低于 -5% 的股票占比（小数）。
        limit_down_ratio (float): 当日日收益低于 -9.5%（近似跌停）的股票
            占比（小数）。
        avg_5d_return (float): 近 5 日近似累积收益，先逐股取 5 日滚动日均
            收益、再取当日截面均值并乘以 5（min_periods=3；数据不足时为
            0.0）。
        total_stocks (int): 当日参与统计的有效股票数。
        trigger_type (str): 触发类型，取值 broad_selloff / extreme_crash /
            cumulative_decline，对应模块 docstring 的三种恐慌形态。
    """
    date: date
    avg_return: float
    decline_5pct_ratio: float
    limit_down_ratio: float
    avg_5d_return: float
    total_stocks: int
    trigger_type: str  # 'broad_selloff' | 'extreme_crash' | 'cumulative_decline'

    @property
    def label(self) -> str:
        """返回恐慌类型对应的中文标签。

        映射：broad_selloff → 普跌型，extreme_crash → 极端暴跌，
        cumulative_decline → 连续阴跌；未知类型原样返回。

        Returns:
            str: 中文标签或原始 trigger_type。
        """
        type_map = {
            'broad_selloff':      '普跌型',
            'extreme_crash':      '极端暴跌',
            'cumulative_decline': '连续阴跌',
        }
        return type_map.get(self.trigger_type, self.trigger_type)


@dataclass
class PositionBatch:
    """一次恐慌事件对应的持仓批次。

    由 run_panic_backtest 建仓并写入买入侧字段，simulate_batch 结算时回填
    退出侧字段（exit_prices / pnl / pnl_pct，并可能改写 exit_date /
    exit_reason）。

    Attributes:
        signal (PanicSignal): 触发本次买入的恐慌信号。
        entry_date (date): 买入日（信号日后第一个交易日，开盘价成交）。
        exit_date (date): 退出日，初始为计划退出日（entry_date 后
            hold_days 个交易日）；触发止损时由 simulate_batch 改写为实际
            止损日。
        stocks (list of str): 实际买入的股票代码。
        entry_prices (dict of str to float): 各股票买入价（信号次日开盘价）。
        shares (dict of str to int): 各股票买入股数（100 的整数倍）。
        cash_invested (float): 投入本金，含买入佣金（建仓时按每股
            price × 1.0001 计入成本）。
        exit_prices (dict of str to float or None, optional): 退出价（计划
            退出日或止损日收盘价），simulate_batch 结算后写入；当日无数据的
            股票记 None。结算前为 None。
        pnl (float or None): 批次总盈亏（已扣卖出佣金），simulate_batch
            结算后写入；结算前为 None。
        pnl_pct (float or None): 盈亏比例（pnl / cash_invested）。
        exit_reason (str): 退出原因：hold_period_end（持有到期，默认）/
            stop_loss（触发止损）/ data_missing（交易日缺失），由
            simulate_batch 改写。
    """
    signal: PanicSignal
    entry_date: date
    exit_date: date                           # 退出日：初始为计划退出日，止损触发时改写为实际止损日
    stocks: List[str]                         # 买入的股票代码
    entry_prices: Dict[str, float]            # 买入价格（信号次日开盘价）
    shares: Dict[str, int]                    # 各股票买入股数（100的整数倍）
    cash_invested: float                      # 投入本金（含买入佣金）
    exit_prices: Optional[Dict[str, float]] = None   # 退出价格（收盘价）
    pnl: Optional[float] = None               # 该批次总盈亏
    pnl_pct: Optional[float] = None           # 盈亏比例
    exit_reason: str = 'hold_period_end'       # 'hold_period_end' | 'stop_loss' | 'data_missing'


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 数据预处理                                                              #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

def build_returns_matrix(
    quote_dfs: Dict[str, pd.DataFrame],
    all_dates: List[date],
    min_dates: int = 100,
) -> pd.DataFrame:
    """构建全市场日收益率宽表（index=date, columns=stock_code）。

    逐股用收盘价 pct_change 计算日收益率并去空后合并为宽表，停牌日为
    NaN；只保留行落在 all_dates 内、且有效数据不少于 min_dates 个交易日的
    股票（上市太晚或停牌过多的股票样本不足，参与恐慌统计会引入噪声）。整体
    由 pandas 向量化完成，性能优于逐日逐股循环。

    Args:
        quote_dfs (dict of str to DataFrame): code → 行情 DataFrame
            （datetime 索引，含 c 列）。
        all_dates (list of date): 需要保留的交易日（并集）。
        min_dates (int): 股票最少有效交易日数，默认 100。

    Returns:
        DataFrame: 收益率宽表（index 为 date、columns 为 code，按日期升序），
            停牌日为 NaN；没有任何股票时返回空 DataFrame。
    """
    # 为每只股票提取日收益率序列
    ret_series = {}
    for code, df in quote_dfs.items():
        s = df['c'].pct_change()
        s = s.dropna()
        # 将 datetime index 转成 date（丢弃盘中时间，按自然日对齐）
        s.index = s.index.date
        ret_series[code] = s

    if not ret_series:
        return pd.DataFrame()

    # 合并所有股票 → 宽表（index=date, columns=code），停牌日为 NaN
    ret_matrix = pd.DataFrame(ret_series)

    # 只保留在 all_dates 内的行
    ret_matrix = ret_matrix[ret_matrix.index.isin(all_dates)]

    # 仅保留至少有 min_dates 个有效数据的股票
    # （上市太晚/停牌过多的股票样本不足，参与恐慌统计会引入噪声）
    ret_matrix = ret_matrix.dropna(axis=1, thresh=min_dates)

    return ret_matrix.sort_index()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 恐慌检测                                                                #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

def detect_all_panics(
    ret_matrix: pd.DataFrame,
    min_stocks: int = 50,
) -> List[PanicSignal]:
    """在全市场收益率矩阵中检测所有恐慌事件。

    对每个日期（行）做截面统计：avg_return 取横截面均值，
    decline_5pct_ratio 为日收益低于 -5% 的占比，limit_down_ratio 为日收益
    低于 -9.5% 的占比；5 日累积收益用逐股 rolling(5, min_periods=3) 日均
    收益的截面均值乘以 5 近似。当日有效股票数不足 min_stocks 的日期直接
    跳过。触发判定如下（命中其一即记为一次信号）：

    - 普跌型：avg_return 低于 -3% 且 decline_5pct_ratio 高于 15%。
    - 极端暴跌：avg_return 低于 -5%。
    - 连续下跌型：5 日累积收益低于 -6% 且 avg_return 低于 -2%。

    Args:
        ret_matrix (DataFrame): 收益率宽表（index=date, columns=code）。
        min_stocks (int): 当日有效股票数下限，默认 50。

    Returns:
        list of PanicSignal: 按日期升序的恐慌信号；矩阵不足 6 行或没有任何
            触发时返回空列表。
    """
    if len(ret_matrix) < 6:
        return []

    # 每行的截面统计
    dates = ret_matrix.index.tolist()
    n_stocks_per_day = ret_matrix.notna().sum(axis=1)  # 当日有效股票数（分母）

    avg_returns = ret_matrix.mean(axis=1)
    decline_5pct = (ret_matrix < -0.05).sum(axis=1) / n_stocks_per_day
    limit_down = (ret_matrix < -0.095).sum(axis=1) / n_stocks_per_day

    # 5日累积收益（滚动平均值求和近似）
    # rolling(5).mean() 得到 5 日滚动日收益均值，×5 ≈ 5 日累积收益
    cum5d = ret_matrix.rolling(5, min_periods=3).mean().mean(axis=1) * 5

    signals: List[PanicSignal] = []

    for i, d in enumerate(dates):
        n = n_stocks_per_day.iloc[i]
        if n < min_stocks:
            continue

        avg_r = avg_returns.iloc[i]
        d5_r = decline_5pct.iloc[i]
        ld_r = limit_down.iloc[i]
        cum5 = cum5d.iloc[i] if i >= 4 else None

        trigger_type = None
        if avg_r < -0.03 and d5_r > 0.15:
            trigger_type = 'broad_selloff'
        elif avg_r < -0.05:
            trigger_type = 'extreme_crash'
        elif cum5 is not None and not pd.isna(cum5) and cum5 < -0.06 and avg_r < -0.02:
            trigger_type = 'cumulative_decline'

        if trigger_type:
            signals.append(PanicSignal(
                date=d,
                avg_return=float(avg_r),
                decline_5pct_ratio=float(d5_r),
                limit_down_ratio=float(ld_r),
                avg_5d_return=float(cum5) if cum5 is not None and not pd.isna(cum5) else 0.0,
                total_stocks=int(n),
                trigger_type=trigger_type,
            ))

    return signals


def dedup_panics(signals: List[PanicSignal], min_gap_days: int = 5) -> List[PanicSignal]:
    """按触发间隔对恐慌信号去重。

    相邻两次触发按自然日计算间隔，不足 min_gap_days 时只保留更早那次，
    避免同一波恐慌在连续几个交易日被重复触发。

    Args:
        signals (list of PanicSignal): 按日期升序的信号列表。
        min_gap_days (int): 两次触发之间的最小自然日间隔，默认 5。

    Returns:
        list of PanicSignal: 去重后的信号列表；输入为空时返回空列表。
    """
    if not signals:
        return []

    result = [signals[0]]
    for sig in signals[1:]:
        gap = (sig.date - result[-1].date).days
        if gap >= min_gap_days:
            result.append(sig)
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 标的选择                                                                #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

def select_stocks(
    quote_dfs: Dict[str, pd.DataFrame],
    signal_date: date,
    top_n: int = 10,
    min_amount: float = 5e6,       # 最小日均成交额 500w
    min_turnover: float = 0.01,    # 最小换手率 1%
    min_history: int = 60,         # 最少历史数据天数
) -> List[Tuple[str, float]]:
    """在恐慌信号日选出 top_n 只逆向标的，返回 (code, score) 降序列表。

    排序公式（三个维度先各自 min-max 归一化到 [0, 1] 再加权，均取越大越
    优先）：

    - Score = 0.5 × norm(5d 跌幅) + 0.3 × norm(20d 均成交额) + 0.2 ×
      norm(1 - 60d 价格位置)，其中跌幅越深、流动性越好、越接近 60 日区间
      低位得分越高；某维度所有候选值相等时归一化结果记 0。

    候选排除条件（基于信号日数据）：

    - 数据长度不足 min_history 天，或信号日不在该股票数据中。
    - 当日日收益不高于 -9.5%（近似跌停），按较前一交易日收盘计算。
    - 换手率（tr 列）低于 min_turnover。
    - 20 日均成交额（信号日含当日往前最多 20 个交易日 a 列均值）低于
      min_amount。

    Args:
        quote_dfs (dict of str to DataFrame): code → 行情 DataFrame
            （datetime 索引，含 o / c / h / l / a / tr 等列）。
        signal_date (date): 恐慌信号日，只取该日及此前数据。
        top_n (int): 返回数量上限，默认 10。
        min_amount (float): 20 日均成交额下限，默认 5e6（500 万）。
        min_turnover (float): 换手率下限，默认 0.01（1%）。
        min_history (int): 最少历史数据天数，默认 60。

    Returns:
        list of tuple of (str, float): 按 score 降序的 (code, score)，最多
            top_n 项；无合格候选时返回空列表。
    """
    candidates: List[Tuple[str, float, float, float]] = []

    for code, df in quote_dfs.items():
        if len(df) < min_history:
            continue

        # 找到信号日当天数据的索引位置
        mask = df.index.date == signal_date
        if not mask.any():
            continue
        idx = df.index.get_loc(df[mask].index[0])
        if isinstance(idx, slice):
            idx = idx.start
        if isinstance(idx, np.ndarray):
            idx = idx[0]

        if idx == 0:
            continue

        # 日收益率（用于排除跌停）
        prev_c = df['c'].iloc[idx - 1]
        curr_c = df['c'].iloc[idx]
        if prev_c <= 0:
            continue
        daily_ret = (curr_c / prev_c) - 1
        if daily_ret <= -0.095:  # 跌停，排除
            continue

        # 换手率
        tr = df['tr'].iloc[idx] if 'tr' in df.columns else None
        if tr is not None and tr < min_turnover:
            continue

        # 20日均成交额
        amount_window = df['a'].iloc[max(0, idx - 19):idx + 1]
        avg_amount = amount_window.mean()
        if avg_amount < min_amount:
            continue

        # 5日跌幅（逆向：跌幅越大越好）
        if idx >= 5:
            ret_5d = (df['c'].iloc[idx] / df['c'].iloc[idx - 5]) - 1
        else:
            ret_5d = 0

        # 60日价格位置
        if idx >= 60:
            high_60 = df['h'].iloc[idx - 59:idx + 1].max()
            low_60 = df['l'].iloc[idx - 59:idx + 1].min()
            price_pos = ((curr_c - low_60) / (high_60 - low_60)) if high_60 > low_60 else 0.5
        else:
            price_pos = 0.5

        candidates.append((code, -ret_5d, avg_amount, 1.0 - price_pos))

    if not candidates:
        return []

    # 归一化并加权
    # 三个维度含义（全部逆向/正向后均取"越大越好"）：
    #   d5_decline = -5日跌幅（跌得越深分数越高，抄底逻辑）
    #   amounts    = 20日均成交额（流动性越好越优先）
    #   inv_pos    = 1 - 60日价格位置（越接近区间低位越优先）
    codes, d5_decline, amounts, inv_pos = zip(*candidates)

    def _norm(vals):
        """对输入序列做 min-max 归一化到 [0, 1]。

        所有值相等时返回全 0，避免除零。

        Args:
            vals (array-like): 待归一化的数值序列。

        Returns:
            numpy.ndarray: 归一化结果。
        """
        arr = np.array(vals, dtype=float)
        min_v, max_v = arr.min(), arr.max()
        if max_v > min_v:
            return (arr - min_v) / (max_v - min_v)
        return np.zeros_like(arr)

    score = (
        0.5 * _norm(d5_decline) +
        0.3 * _norm(amounts) +
        0.2 * _norm(inv_pos)
    )

    ranked = sorted(zip(codes, score), key=lambda x: x[1], reverse=True)
    return ranked[:top_n]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 持仓模拟                                                                #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

def find_next_trading_date(all_dates: List[date], from_date: date, offset: int) -> Optional[date]:
    """在交易日列表中定位 from_date 之后第 offset 个交易日。

    offset=1 表示 from_date 的下一个交易日，offset=N 表示其后的第 N 个
    交易日。from_date 不在列表中时，把列表里第一个晚于 from_date 的日期
    视为已跳过的一天（等价于先跳一天再后移 offset - 1 个）。

    Args:
        all_dates (list of date): 升序交易日列表。
        from_date (date): 基准日期。
        offset (int): 向后偏移的交易日个数。

    Returns:
        date or None: 目标交易日；剩余交易日不足 offset 个（或列表中不存在
            晚于 from_date 的日期）时返回 None。
    """
    try:
        idx = all_dates.index(from_date)
    except ValueError:
        # from_date 不在列表中，找第一个大于它的
        idx = next((i for i, d in enumerate(all_dates) if d > from_date), -1)
        if idx < 0:
            return None
        offset -= 1  # 已经跳过一个

    target = idx + offset
    if target < len(all_dates):
        return all_dates[target]
    return None


def get_stock_price_on_date(
    df: pd.DataFrame, dt: date, price_col: str = 'c'
) -> Optional[float]:
    """取某只股票在指定日期的行情值（默认收盘价）。

    Args:
        df (DataFrame): 行情 DataFrame（datetime 索引）。
        dt (date): 目标日期。
        price_col (str): 取值列名，默认 'c'（收盘价）；调用方也可传 'o'
            取开盘价。

    Returns:
        float or None: 当日该列的值；当日无数据（停牌 / 未上市）或值不大于
            0 时返回 None。
    """
    mask = df.index.date == dt
    if not mask.any():
        return None
    val = df[mask][price_col].iloc[0]
    return float(val) if val > 0 else None


def simulate_batch(
    batch: PositionBatch,
    quote_dfs: Dict[str, pd.DataFrame],
    all_dates: List[date],
    stop_loss: float,
    commission_buy: float = 0.0001,
    commission_sell: float = 0.0011,
) -> None:
    """模拟一个批次的持仓：逐日估值、检查止损，到期或止损时结算。

    从 entry_date 起逐日（含 exit_date）用收盘价对持仓估值；某日有股票
    停牌（无数据）且未到退出日时跳过该日检查。启用止损（stop_loss 大于 0）
    时，若当日组合总市值相对 batch.cash_invested 的跌幅超过 stop_loss，
    则把 batch.exit_date 改写为该日、exit_reason 置为 stop_loss 并按该日
    收盘价退出；否则持有到原 exit_date 收盘退出。止损判断受日线数据限制用
    当日收盘价执行，实盘应在盘中执行。

    结算时按退出日收盘价计算市值（当日无数据的股票记 None 且不计入市值），
    按卖出费率 commission_sell 扣费（卖出市值 × (1 - commission_sell) 为净
    回款）后写回 batch.pnl / pnl_pct；买入佣金已在建仓时计入
    cash_invested。entry_date 或 exit_date 不在 all_dates 中时，把
    exit_reason 置为 data_missing、pnl / pnl_pct 置 0 后直接返回。本函数
    无返回值，结果以副作用形式写回 batch（exit_date / exit_reason /
    exit_prices / pnl / pnl_pct）。

    Args:
        batch (PositionBatch): 待模拟的批次，退出侧字段写回该对象。
        quote_dfs (dict of str to DataFrame): code → 行情 DataFrame。
        all_dates (list of date): 升序交易日列表。
        stop_loss (float): 止损阈值（>0 启用，按总市值相对投入本金的亏损
            比例判定）。
        commission_buy (float): 买入佣金费率，默认 0.0001（当前实现未在
            本函数内使用，买入成本已含于 cash_invested）。
        commission_sell (float): 卖出费率（含印花税），默认 0.0011。
    """
    try:
        entry_idx = all_dates.index(batch.entry_date)
        exit_idx = all_dates.index(batch.exit_date)
    except ValueError:
        batch.exit_reason = 'data_missing'
        batch.pnl = 0
        batch.pnl_pct = 0
        return

    # 逐日检查
    for i in range(entry_idx, exit_idx + 1):
        dt = all_dates[i]

        # 计算当日组合市值
        total_value = 0.0
        all_have_price = True
        for code in list(batch.stocks):
            if code not in quote_dfs:
                continue
            price = get_stock_price_on_date(quote_dfs[code], dt, 'c')
            if price is None:
                all_have_price = False
                continue
            total_value += batch.shares.get(code, 0) * price

        if not all_have_price and i < exit_idx:
            continue  # 停牌等，跳过

        # 检查止损
        if stop_loss > 0:
            drawdown = total_value / batch.cash_invested - 1
            if drawdown < -stop_loss and i >= entry_idx:
                # 触发止损：用当日收盘价退出
                batch.exit_date = dt
                batch.exit_reason = 'stop_loss'
                break

    # 计算最终退出价格和 PnL
    batch.exit_prices = {}
    exit_value = 0.0
    for code in batch.stocks:
        if code not in quote_dfs:
            batch.exit_prices[code] = None
            continue
        # 以计划退出日（或止损日）收盘价卖出
        price = get_stock_price_on_date(quote_dfs[code], batch.exit_date, 'c')
        batch.exit_prices[code] = price
        if price is not None:
            exit_value += batch.shares.get(code, 0) * price

    # 卖出佣金（含印花税）：净回款 = 卖出市值 × (1 - 卖出费率)
    exit_value_after_comm = exit_value * (1 - commission_sell)
    batch.pnl = exit_value_after_comm - batch.cash_invested
    batch.pnl_pct = batch.pnl / batch.cash_invested if batch.cash_invested > 0 else 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 主回测运行器                                                            #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

@dataclass
class PanicBacktestResult:
    """极端恐慌逆向策略一次回测的结果与统计。

    通常由 run_panic_backtest() 构造并回填：参数组字段来自调用参数，
    结果组字段（signals / batches / final_value / 净值曲线）由回测主流程
    汇总；区间内无恐慌信号时，结果组退化为净值恒等于初始资金的空结果。

    Attributes:
        strategy_name (str): 策略名，固定为 ExtremePanicContrarian（字段
            默认值，未被回填）。
        initial_cash (float): 初始资金，默认 1,000,000。
        start_date (date or None): 回测起始日期，None 表示取数据最早交易日。
        end_date (date or None): 回测结束日期，None 表示取数据最晚交易日。
        hold_days (int): 每批持仓交易日数，默认 5。
        top_n (int): 每批最多买入的股票只数，默认 10。
        stop_loss (float): 止损阈值，默认 0.10。
        deploy_pct (float): 每次触发时投入可用资金的比例，默认 1.0。
        signals (list of PanicSignal): 去重后的恐慌信号列表。
        batches (list of PositionBatch): 已结算的持仓批次，按买入日升序。
        final_value (float): 回测结束时的账户净值。
        portfolio_dates (list of date): 每日净值曲线的日期序列。
        portfolio_values (list of float): 与 portfolio_dates 对应的每日
            净值序列。
    """
    strategy_name: str = 'ExtremePanicContrarian'
    initial_cash: float = 1_000_000
    start_date: Optional[date] = None
    end_date: Optional[date] = None

    # 参数
    hold_days: int = 5
    top_n: int = 10
    stop_loss: float = 0.10
    deploy_pct: float = 1.0

    # 结果
    signals: List[PanicSignal] = field(default_factory=list)
    batches: List[PositionBatch] = field(default_factory=list)
    final_value: float = 0.0

    # 净值曲线（每日）
    portfolio_dates: List[date] = field(default_factory=list)
    portfolio_values: List[float] = field(default_factory=list)

    # ── 统计指标 ─────────────────────────────────────────────────── #

    @property
    def total_return(self) -> float:
        """总收益率：最终净值相对初始资金的净收益。

        Returns:
            float: ``final_value / initial_cash - 1`` （小数）；initial_cash
                不大于 0 时返回 0。
        """
        return self.final_value / self.initial_cash - 1 if self.initial_cash > 0 else 0

    @property
    def cagr(self) -> float:
        """年化复合收益率，按首尾日期的实际跨度年化。

        日期不足两个或首尾跨度为 0 时返回 0.0；一年按 365.25 个自然日计。

        Returns:
            float: ``(final_value / initial_cash) ** (1 / n_years) - 1``。
        """
        if not self.portfolio_dates or len(self.portfolio_dates) < 2:
            return 0.0
        n_years = (self.portfolio_dates[-1] - self.portfolio_dates[0]).days / 365.25
        if n_years <= 0:
            return 0.0
        return (self.final_value / self.initial_cash) ** (1 / n_years) - 1

    @property
    def max_drawdown(self) -> float:
        """最大回撤：净值从历史峰值到谷值的最大跌幅。

        Returns:
            float: 峰值到谷值的最小净值相对峰值收益（负数，越接近 0 表示
                回撤越小）。
        """
        peak = self.initial_cash
        max_dd = 0.0
        for v in self.portfolio_values:
            if v > peak:
                peak = v
            dd = v / peak - 1
            if dd < max_dd:
                max_dd = dd
        return max_dd

    @property
    def sharpe(self) -> float:
        """年化 Sharpe 比率，按逐日净值收益率序列计算。

        无风险利率取 2.5%（日化按 252 个交易日折算），年化乘子为 252 的
        平方根；观测值不足两个或收益率标准差为 0 时返回 0.0。

        Returns:
            float: 年化 Sharpe 比率。
        """
        if len(self.portfolio_values) < 2:
            return 0.0
        rets = [
            self.portfolio_values[i] / self.portfolio_values[i - 1] - 1
            for i in range(1, len(self.portfolio_values))
        ]
        if not rets:
            return 0.0
        mean_r = statistics.mean(rets)
        std_r = statistics.stdev(rets) if len(rets) > 1 else 0
        if std_r == 0:
            return 0.0
        return (mean_r - 0.025 / 252) / std_r * math.sqrt(252)

    @property
    def win_rate(self) -> float:
        """盈利批次占全部批次的比重。

        Returns:
            float: 批次中 pnl 大于 0 的比例（小数）；无批次时返回 0.0。
        """
        if not self.batches:
            return 0.0
        wins = sum(1 for b in self.batches if b.pnl and b.pnl > 0)
        return wins / len(self.batches)

    @property
    def avg_return_per_trade(self) -> float:
        """单批平均收益率：各批次 pnl_pct 的算术平均。

        Returns:
            float: 有 pnl_pct 的批次的算术平均；无批次或均无 pnl_pct 时
                返回 0.0。
        """
        if not self.batches:
            return 0.0
        pcts = [b.pnl_pct for b in self.batches if b.pnl_pct is not None]
        return statistics.mean(pcts) if pcts else 0.0

    # ── 输出函数 ─────────────────────────────────────────────────── #

    def print_report(self):
        """在控制台打印回测统计报告。

        内容包括策略 / 周期 / 持仓参数配置与初始资金、最终净值、总收益率、
        年化收益、最大回撤、Sharpe、总批次、胜率与单批平均收益；有批次时
        另按恐慌触发类型分组打印次数、胜率与平均收益。
        """
        print()
        print('─' * 70)
        print(f'  策略: 极端恐慌逆向策略 (Panic Contrarian)')
        print(f'  周期: {self.start_date} ~ {self.end_date}')
        print(f'  持有天数: {self.hold_days}d  |  每批持仓: {self.top_n}只  '
              f'|  止损: {self.stop_loss:.0%}  |  资金使用率: {self.deploy_pct:.0%}')
        print('─' * 70)
        print(f'  初始资金:    {self.initial_cash:>14,.0f}')
        print(f'  最终净值:    {self.final_value:>14,.2f}')
        print(f'  总收益率:    {self.total_return:>+14.2%}')
        print(f'  年化收益:    {self.cagr:>+14.2%}')
        print(f'  最大回撤:    {self.max_drawdown:>14.2%}')
        print(f'  Sharpe:      {self.sharpe:>14.2f}')
        print(f'  总批次:      {len(self.batches):>14}')
        print(f'  胜率:        {self.win_rate:>14.1%}')
        print(f'  平均每批收益: {self.avg_return_per_trade:>+13.2%}')
        print('─' * 70)

        # 按触发类型分类统计
        if self.batches:
            print()
            print('  按恐慌类型分类:')
            from collections import Counter
            by_type = Counter(b.signal.trigger_type for b in self.batches)
            for t, cnt in by_type.items():
                type_batches = [b for b in self.batches if b.signal.trigger_type == t]
                avg_pct = statistics.mean(
                    [b.pnl_pct for b in type_batches if b.pnl_pct is not None]
                )
                wins = sum(1 for b in type_batches if b.pnl and b.pnl > 0)
                print(f'    {t:20s}  次数:{cnt:3d}  胜率:{wins/cnt:5.1%}  '
                      f'均收益:{avg_pct:+6.2%}')
        print()

    def save_trades(self, output_dir: Path) -> Path:
        """把每批交易明细写入 panic_trades.csv 并返回文件路径。

        目录不存在时自动创建；每个批次写一行，列为 entry_date / exit_date /
        trigger_type / signal_avg_return / num_stocks / stocks /
        cash_invested / pnl / pnl_pct / exit_reason（pnl 或 pnl_pct 为
        None 时留空）。

        Args:
            output_dir (Path or str): 输出目录。

        Returns:
            Path: panic_trades.csv 的完整路径。
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / 'panic_trades.csv'

        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'entry_date', 'exit_date', 'trigger_type', 'signal_avg_return',
                'num_stocks', 'stocks', 'cash_invested', 'pnl', 'pnl_pct',
                'exit_reason',
            ])
            for b in self.batches:
                writer.writerow([
                    b.entry_date.isoformat(),
                    b.exit_date.isoformat(),
                    b.signal.trigger_type,
                    f'{b.signal.avg_return:.4f}',
                    len(b.stocks),
                    ','.join(b.stocks),
                    f'{b.cash_invested:.2f}',
                    f'{b.pnl:.2f}' if b.pnl is not None else '',
                    f'{b.pnl_pct:.4f}' if b.pnl_pct is not None else '',
                    b.exit_reason,
                ])
        return path

    def save_daily_nav(self, output_dir: Path) -> Path:
        """把每日净值写入 panic_nav.csv 并返回文件路径。

        目录不存在时自动创建；按 portfolio_dates 与 portfolio_values 逐日
        写两列：date（isoformat）与 value。

        Args:
            output_dir (Path or str): 输出目录。

        Returns:
            Path: panic_nav.csv 的完整路径。
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / 'panic_nav.csv'

        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['date', 'value'])
            for d, v in zip(self.portfolio_dates, self.portfolio_values):
                writer.writerow([d.isoformat(), f'{v:.2f}'])
        return path

    def save_plot(self, output_dir: Path) -> Path:
        """保存归一化资金曲线图到 panic_equity_curve.png 并返回路径。

        上下两段子图：上为归一化净值曲线（按批次建仓 / 退出日标注事件点，
        红绿区分盈亏，并高亮最大回撤区间），下为每日回撤面积图；
        portfolio_dates 为空时只创建目录并返回路径，不生成图片。

        Args:
            output_dir (Path or str): 输出目录。

        Returns:
            Path: panic_equity_curve.png 的完整路径。
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / 'panic_equity_curve.png'

        if not self.portfolio_dates:
            return path

        s = pd.Series(self.portfolio_values, index=self.portfolio_dates)
        normalized = s / s.iloc[0]

        fig, axes = plt.subplots(2, 1, figsize=(14, 8),
                                 gridspec_kw={'height_ratios': [3, 1]})

        # 上：净值曲线
        ax1 = axes[0]
        ax1.plot(normalized.index, normalized.values,
                 color='steelblue', linewidth=1.5, label='Panic Contrarian')

        # 标注恐慌事件
        for b in self.batches:
            if b.entry_date in normalized.index:
                y = normalized.loc[b.entry_date]
                color = 'green' if b.pnl and b.pnl > 0 else 'red'
                ax1.scatter(b.entry_date, y, color=color, s=30, zorder=5, alpha=0.7)
                ax1.scatter(b.exit_date,
                            normalized.get(b.exit_date, y),
                            color=color, s=15, zorder=5, alpha=0.5, marker='x')

        # 最大回撤标注
        peak_val = normalized.iloc[0]
        peak_idx = 0
        max_dd = 0.0
        max_dd_start = 0
        max_dd_end = 0
        current_peak_idx = 0
        current_peak = normalized.iloc[0]
        current_trough = normalized.iloc[0]
        for i, v in enumerate(normalized):
            if v > current_peak:
                current_peak = v
                current_peak_idx = i
                current_trough = v
            if v < current_trough:
                current_trough = v
                dd = current_trough / current_peak - 1
                if dd < max_dd:
                    max_dd = dd
                    max_dd_start = current_peak_idx
                    max_dd_end = i

        if max_dd < 0:
            ax1.axvspan(normalized.index[max_dd_start],
                        normalized.index[max_dd_end],
                        alpha=0.12, color='red', label=f'MaxDD: {max_dd:.1%}')

        ax1.axhline(y=1.0, color='gray', linestyle='--', linewidth=0.8, alpha=0.5)
        ax1.set_ylabel('Normalized NAV')
        ax1.set_title(
            f'[Extreme Panic Contrarian]  hold={self.hold_days}d  top_n={self.top_n}  '
            f'{self.start_date} ~ {self.end_date}  '
            f'Return {self.total_return:+.1%}  CAGR {self.cagr:+.1%}  '
            f'MaxDD {self.max_drawdown:.1%}  Sharpe {self.sharpe:.2f}  '
            f'WinRate {self.win_rate:.1%}',
            fontsize=9
        )
        ax1.legend(loc='upper left', fontsize=8)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%y-%m'))
        ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha='right')

        # 下：每日回撤
        ax2 = axes[1]
        dd_series = normalized / normalized.cummax() - 1
        ax2.fill_between(dd_series.index, dd_series.values,
                         0, color='red', alpha=0.4)
        ax2.set_ylabel('Drawdown')
        ax2.set_ylim(min(dd_series.min() * 1.1, -0.01), 0.01)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%y-%m'))
        ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30, ha='right')

        plt.tight_layout()
        plt.savefig(path, dpi=150, bbox_inches='tight')
        plt.close(fig)
        return path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #
# 主流程                                                                  #
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ #

def run_panic_backtest(
    data_root: str | Path,
    hold_days: int = 5,
    top_n: int = 10,
    stop_loss: float = 0.10,
    deploy_pct: float = 1.0,
    initial_cash: float = 1_000_000,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    min_stocks: int = 100,
    min_gap_days: int = 5,
    verbose_cb=None,
) -> PanicBacktestResult:
    """极端恐慌逆向策略回测主入口：加载数据、检测恐慌并逐信号模拟持仓。

    主流程：加载 quote/d/stock/ 下全部 parquet 行情 → 汇总交易日（并集，
    可按 start_date / end_date 截取）→ 构建收益率矩阵 → 检测并去重恐慌
    信号 → 对每个信号在次一交易日开盘建仓（按可用资金等权分配，每只买入
    100 股整数倍），持有 hold_days 个交易日后于收盘卖出（持有期内可触发
    止损，止损判定与退出口径见 simulate_batch）→ 把每批的建仓 / 退出现金
    流事件按时间叠加到现金上，得到每日净值曲线并汇总为 PanicBacktestResult。
    仓位不足 3 只或信号后无交易日 / 无足够持有期的信号被跳过；区间内无恐慌
    信号时返回净值恒等于初始资金的空结果。

    Args:
        data_root (str or Path): 数据根目录（包含 quote/d/stock/ 子目录）。
        hold_days (int): 每批持仓交易日数（触发后第 N 个交易日收盘卖出），
            默认 5。
        top_n (int): 每批最多买入的股票只数，默认 10。
        stop_loss (float): 止损阈值（>0 启用，按总市值相对买入成本的亏损
            比例判定），默认 0.10。
        deploy_pct (float): 每次触发时投入当前可用资金的比例（0~1），默认
            1.0。
        initial_cash (float): 初始资金，默认 1,000,000。
        start_date (date, optional): 回测起始日期（含）；None 表示取数据
            最早交易日。
        end_date (date, optional): 回测结束日期（含）；None 表示取数据最晚
            交易日。
        min_stocks (int): 恐慌检测所需的最少有效股票数，默认 100。
        min_gap_days (int): 相邻恐慌触发之间的最小自然日间隔（去重用），
            默认 5。
        verbose_cb (callable, optional): 进度回调 verbose_cb(stage: str,
            info)，stage 如 loading / matrix / detecting / signals /
            trading / progress / done 等。

    Returns:
        PanicBacktestResult: 回测结果（信号、批次、净值曲线与统计指标）。

    Raises:
        ValueError: 未找到行情数据，或合并后交易日不足 60 天时抛出。
    """
    data_root = Path(data_root)

    # ── 1. 加载数据 ──────────────────────────────────────────────
    if verbose_cb:
        verbose_cb('loading', str(data_root))
    quote_dfs = load_all_quote_dfs_parquet(data_root)
    if not quote_dfs:
        raise ValueError(f'未找到行情数据: {data_root}/quote/d/')

    all_dates = get_all_trading_dates_from_dfs(quote_dfs, start_date, end_date)
    if len(all_dates) < 60:
        raise ValueError(f'交易日不足: {len(all_dates)}')

    # ── 2. 构建收益率矩阵 ────────────────────────────────────────
    if verbose_cb:
        verbose_cb('matrix', f'{len(quote_dfs)} stocks, {len(all_dates)} dates')
    ret_matrix = build_returns_matrix(quote_dfs, all_dates, min_dates=100)

    # ── 3. 检测恐慌事件 ──────────────────────────────────────────
    if verbose_cb:
        verbose_cb('detecting', '')
    all_signals = detect_all_panics(ret_matrix, min_stocks=min_stocks)
    signals = dedup_panics(all_signals, min_gap_days=min_gap_days)

    if verbose_cb:
        verbose_cb('signals', f'{len(all_signals)} raw → {len(signals)} deduplicated')

    if not signals:
        if verbose_cb:
            verbose_cb('nosignals', '未检测到任何恐慌事件')
        result = PanicBacktestResult(
            initial_cash=initial_cash,
            start_date=all_dates[0], end_date=all_dates[-1],
            hold_days=hold_days, top_n=top_n, stop_loss=stop_loss,
            deploy_pct=deploy_pct,
            final_value=initial_cash,
            portfolio_dates=all_dates,
            portfolio_values=[initial_cash] * len(all_dates),
        )
        return result

    # ── 4. 模拟交易 ──────────────────────────────────────────────
    if verbose_cb:
        verbose_cb('trading', f'{len(signals)} signals')

    free_cash = initial_cash
    active_batches: List[PositionBatch] = []   # 持有中的批次
    completed_batches: List[PositionBatch] = []

    # 每日净值追踪
    daily_nav: Dict[date, float] = {}

    # 初始日期的净值
    first_date = all_dates[0]
    daily_nav[first_date] = initial_cash

    for sig_idx, sig in enumerate(signals):
        # 找到恐慌日之后第一个交易日（买入日）
        entry_date = find_next_trading_date(all_dates, sig.date, 1)
        if entry_date is None:
            continue

        # 找到退出日（entry_date 之后 hold_days 个交易日）
        exit_date = find_next_trading_date(all_dates, entry_date, hold_days)
        if exit_date is None:
            continue

        # 先处理在此 entry_date 之前到期的批次（释放资金）
        for b in active_batches[:]:  # iterate copy
            if b.exit_date < entry_date:
                # 模拟此批次
                simulate_batch(b, quote_dfs, all_dates, stop_loss)
                free_cash += (b.cash_invested + (b.pnl or 0))
                completed_batches.append(b)
                active_batches.remove(b)

        # 选出标的
        ranked_stocks = select_stocks(quote_dfs, sig.date, top_n=top_n)
        if len(ranked_stocks) < 3:
            continue  # 太少，放弃本次信号

        # 计算投入资金
        cash_to_deploy = free_cash * deploy_pct
        per_stock_cash = cash_to_deploy / len(ranked_stocks)

        batch_stocks = []
        entry_prices = {}
        shares = {}
        total_used = 0.0

        for code, score in ranked_stocks:
            if code not in quote_dfs:
                continue
            price = get_stock_price_on_date(quote_dfs[code], entry_date, 'o')
            if price is None or price <= 0:
                continue
            n_shares = int(per_stock_cash / price / 100) * 100
            if n_shares < 100:
                continue
            cost = n_shares * price * (1 + 0.0001)  # 含买入佣金
            entry_prices[code] = price
            shares[code] = n_shares
            total_used += cost
            batch_stocks.append(code)

        if len(batch_stocks) < 3:
            continue

        batch = PositionBatch(
            signal=sig,
            entry_date=entry_date,
            exit_date=exit_date,
            stocks=batch_stocks,
            entry_prices=entry_prices,
            shares=shares,
            cash_invested=total_used,
        )
        active_batches.append(batch)
        free_cash -= total_used

        if verbose_cb and sig_idx % 20 == 0:
            verbose_cb('progress', f'{sig_idx}/{len(signals)} signals, '
                       f'{len(completed_batches)} completed, free_cash={free_cash:,.0f}')

    # 处理所有剩余活跃批次
    for b in active_batches:
        simulate_batch(b, quote_dfs, all_dates, stop_loss)
        if b.pnl is not None:
            free_cash += (b.cash_invested + b.pnl)
        else:
            free_cash += b.cash_invested  # fallback
        completed_batches.append(b)
    active_batches.clear()

    # ── 5. 构建完整净值曲线 ─────────────────────────────────────
    sorted_batches = sorted(completed_batches, key=lambda b: b.entry_date)

    all_dates_bt = all_dates[:]
    nav_series: Dict[date, float] = {}
    current_cash = initial_cash

    # 把每一批看成独立的现金流事件：
    #   entry_date 发生 invest（现金流出 = cash_invested）
    #   exit_date  发生 return（现金流入 = cash_invested + pnl）
    # 再按时间顺序把事件叠加到现金上，得到每日净值
    events = []
    for b in sorted_batches:
        events.append((b.entry_date, 'invest', b.cash_invested))
        events.append((b.exit_date, 'return', b.cash_invested + (b.pnl or 0)))

    events.sort(key=lambda x: x[0])
    event_idx = 0

    for dt in all_dates_bt:
        # 处理发生在 dt 之前的所有现金流事件（同日事件在下一轮循环处理）
        while event_idx < len(events) and events[event_idx][0] < dt:
            _, ev_type, amount = events[event_idx]
            if ev_type == 'invest':
                current_cash -= amount
            else:
                current_cash += amount
            event_idx += 1

        # 估算当日持仓市值（简单起见：持有中的批次用成本价估算）
        # 更准确的做法：对每个活跃批次，计算当日收盘市值
        # 这里简化：用投资成本替代（曲线会平滑，但不影响终值）
        active_value = 0.0
        for b in sorted_batches:
            if b.entry_date <= dt <= b.exit_date:
                # 用投资成本近似（简化）
                active_value += b.cash_invested

        nav_series[dt] = current_cash + active_value

    portfolio_dates = list(nav_series.keys())
    portfolio_values = list(nav_series.values())
    final_value = portfolio_values[-1] if portfolio_values else initial_cash

    result = PanicBacktestResult(
        initial_cash=initial_cash,
        start_date=all_dates[0],
        end_date=all_dates[-1],
        hold_days=hold_days,
        top_n=top_n,
        stop_loss=stop_loss,
        deploy_pct=deploy_pct,
        signals=signals,
        batches=sorted_batches,
        final_value=final_value,
        portfolio_dates=portfolio_dates,
        portfolio_values=portfolio_values,
    )

    if verbose_cb:
        verbose_cb('done', f'Return={result.total_return:+.2%}, '
                   f'Signals={len(signals)}, Batches={len(sorted_batches)}')

    return result


def run_panic_sweep(
    data_root: str | Path,
    hold_days_list=(3, 5, 7, 10),
    top_n_list=(5, 10),
    stop_loss: float = 0.10,
    initial_cash: float = 1_000_000,
    **kwargs,
) -> List[PanicBacktestResult]:
    """参数网格搜索：遍历 hold_days × top_n 组合逐一跑回测。

    对每个组合调用 run_panic_backtest 并在控制台实时打印进度与结果摘要；
    单个组合抛异常时打印 FAILED 后继续，不影响其余组合。

    Args:
        data_root (str or Path): 数据根目录。
        hold_days_list (iterable of int): 待试的持仓天数，默认 (3, 5, 7,
            10)。
        top_n_list (iterable of int): 待试的买入只数，默认 (5, 10)。
        stop_loss (float): 止损阈值，默认 0.10。
        initial_cash (float): 初始资金，默认 1,000,000。
        **kwargs: 其余参数原样透传给 run_panic_backtest（如 start_date /
            end_date / min_stocks / min_gap_days / verbose_cb 等）。

    Returns:
        list of PanicBacktestResult: 成功完成的所有组合结果，顺序为
            hold_days 外层、top_n 内层；失败的组合被跳过。
    """
    results = []
    for hold in hold_days_list:
        for tn in top_n_list:
            print(f'\n  ▶ hold={hold}d  top_n={tn}  ...', end=' ', flush=True)
            try:
                r = run_panic_backtest(
                    data_root=data_root,
                    hold_days=hold,
                    top_n=tn,
                    stop_loss=stop_loss,
                    initial_cash=initial_cash,
                    **kwargs,
                )
                results.append(r)
                print(f'Return={r.total_return:+.2%}  '
                      f'CAGR={r.cagr:+.2%}  MaxDD={r.max_drawdown:.2%}  '
                      f'Batches={len(r.batches)}  WinRate={r.win_rate:.1%}')
            except Exception as e:
                print(f'FAILED: {e}')
    return results


def print_sweep_summary(results: List[PanicBacktestResult]):
    """在控制台打印参数扫描结果汇总表。

    按 (hold_days, top_n) 升序逐行打印各组合的收益、CAGR、最大回撤、
    Sharpe、批次数量、胜率与单批平均收益。

    Args:
        results (list of PanicBacktestResult): 待汇总的回测结果列表。
    """
    print()
    print('=' * 90)
    print(f'  {"Hold":>6s}  {"TopN":>5s}  '
          f'{"Return":>8s}  {"CAGR":>8s}  {"MaxDD":>8s}  '
          f'{"Sharpe":>7s}  {"Batches":>7s}  {"WinRate":>7s}  '
          f'{"AvgRet":>8s}')
    print('-' * 90)
    for r in sorted(results, key=lambda x: (x.hold_days, x.top_n)):
        print(f'  {r.hold_days:>5d}d  {r.top_n:>5d}  '
              f'{r.total_return:>+7.1%}  {r.cagr:>+7.1%}  {r.max_drawdown:>+7.1%}  '
              f'{r.sharpe:>6.2f}  {len(r.batches):>7d}  '
              f'{r.win_rate:>6.1%}  {r.avg_return_per_trade:>+7.1%}')
    print('=' * 90)
    print()
