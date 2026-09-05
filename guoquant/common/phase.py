"""
市场情绪阶段模块：依据情绪强度与动量判定市场所处阶段。

情绪阶段由两个参数决定：

- ``E``：情绪强度百分位，取值 [0, 1]，当前情绪在近 240 日历史序列中
  的相对位置；
- ``M``：情绪动量（当前 ``E`` 较 3 日前 ``E`` 的变化），反映情绪方向。

阶段判定（阈值与顺序同 ``get_phase``）：

- ``frozen`` （冰点）：``E`` < 0.1 且 ``M`` ≤ 0——市场极度低迷，无赚钱
  效应，空仓；
- ``recovery`` （修复）：``E`` < 0.3 且 ``M`` > 0——情绪开始回暖，
  低吸龙头；
- ``ferment`` （发酵）：0.3 ≤ ``E`` < 0.7 且 ``M`` > 0——主力资金入场，
  积极布局；
- ``climax`` （高潮）：``E`` ≥ 0.7 且 ``M`` > 0——情绪顶部，只做最强
  主线；
- ``retreat`` （退潮）：``E`` ≥ 0.7 且 ``M`` < 0——高位转向，清仓防守；
- ``unknown`` （未知）：其余未匹配情形，如 ``E`` ≥ 0.1 且 ``M`` = 0
  （动量归零）、或 0.1 ≤ ``E`` < 0.7 且 ``M`` ≤ 0 的横盘/转弱段。

策略适用阶段（键为板块分支股票策略的函数全名，即策略名）：

- ``stock_lowend_startup_strategy`` / ``stock_ma_cluster_breakout_strategy``
  → 修复、发酵；
- ``stock_pullback_strategy`` / ``stock_strength_strategy`` /
  ``stock_flow_strategy`` / ``stock_main_wave_strategy`` → 发酵、高潮。

rise_rate / user_momentum 等无板块策略与 theme_strength 不做阶段过滤，
故不在表中。

与其他模块的关系：

- 生产方：``guoquant/commands/check_market_sentiment_cycle.py`` 调用
  ``guoquant/strategies/market_sentiment_cycle_strategy`` 计算 E/M，
  写入 ``out/<日期>/rank/market_sentiment_cycle.txt``；
- 消费方：``guoquant/commands/rank.py`` 用 ``read_market_phase`` 读回
  (E, M, 阶段)，以 ``STRATEGY_PHASES`` 校验策略适用阶段（不适用仅提示）。
"""

PHASE_FROZEN   = 'frozen'
PHASE_RECOVERY = 'recovery'
PHASE_FERMENT  = 'ferment'
PHASE_CLIMAX   = 'climax'
PHASE_RETREAT  = 'retreat'
PHASE_UNKNOWN  = 'unknown'

PHASE_NAMES = {
    PHASE_FROZEN:   '冰点',
    PHASE_RECOVERY: '修复',
    PHASE_FERMENT:  '发酵',
    PHASE_CLIMAX:   '高潮',
    PHASE_RETREAT:  '退潮',
    PHASE_UNKNOWN:  '未知',
}

STRATEGY_PHASES = {
    'stock_lowend_startup_strategy':  [PHASE_RECOVERY, PHASE_FERMENT],
    'stock_ma_cluster_breakout_strategy': [PHASE_RECOVERY, PHASE_FERMENT],
    'stock_pullback_strategy':       [PHASE_FERMENT, PHASE_CLIMAX],
    'stock_strength_strategy':       [PHASE_FERMENT, PHASE_CLIMAX],
    'stock_flow_strategy':           [PHASE_FERMENT, PHASE_CLIMAX],
    'stock_main_wave_strategy':      [PHASE_FERMENT, PHASE_CLIMAX],
}


def get_phase(e, m):
    """根据情绪强度 ``E`` 和动量 ``M`` 返回当前阶段常量。

    判定顺序与各阶段阈值见模块 docstring 的阶段定义；边界情形：
    ``E`` ≥ 0.7 且 ``M`` = 0（动量归零）、或 0.1 ≤ ``E`` < 0.7 且
    ``M`` ≤ 0 时均落入 unknown，``E`` < 0.1 且 ``M`` > 0 因满足
    ``E`` < 0.3 而归入 recovery。

    Args:
        e (float): 情绪强度百分位，范围 [0, 1]（近 240 日历史位置）。
        m (float): 情绪动量，``E`` 相对 3 日前的变化（可为负）。

    Returns:
        str: 对应阶段常量，为 ``PHASE_FROZEN`` / ``PHASE_RECOVERY`` /
        ``PHASE_FERMENT`` / ``PHASE_CLIMAX`` / ``PHASE_RETREAT`` /
        ``PHASE_UNKNOWN`` 之一（值见各 ``PHASE_*`` 定义）。
    """
    if e < 0.1 and m <= 0:        return PHASE_FROZEN
    if e < 0.3 and m > 0:         return PHASE_RECOVERY
    if 0.3 <= e < 0.7 and m > 0:  return PHASE_FERMENT
    if e >= 0.7 and m > 0:        return PHASE_CLIMAX
    if e >= 0.7 and m < 0:        return PHASE_RETREAT
    return PHASE_UNKNOWN


def read_market_phase(path):
    """从 ``market_sentiment_cycle.txt`` 读取当前情绪阶段。

    文件由 ``commands/check_market_sentiment_cycle.py`` 生成，共两行：
    首行为情绪强度原始值，末行为 E/M 行（格式 ``E=0.52, M=0.012``），
    本函数取末行解析。

    Args:
        path (str): ``market_sentiment_cycle.txt`` 路径。

    Returns:
        tuple[float, float, str]: (e, m, phase)——浮点情绪强度、浮点动量，
        以及 ``get_phase`` 判定的对应阶段常量。
    """
    with open(path) as fp:
        line = fp.readlines()[-1]
    e_str, m_str = line.split(',')
    e = float(e_str.split('=')[1])
    m = float(m_str.split('=')[1])
    return e, m, get_phase(e, m)
