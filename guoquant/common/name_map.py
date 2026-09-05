"""排名产物命名映射：rank CSV 文件名 stem → 中文显示名。

键与 rank 命令的输出文件 stem 一一对应，覆盖主题排名
（``{type}_theme_strength_score.csv``）、板块分支股票排名
（``{type}_{策略}_score.csv``，概念/行业两类）与无板块策略
（``{策略}_score.csv``），见 ``guoquant/commands/rank.py`` 的命名约定；值
用于 report / trace 的前端展示。

Attributes:
    strategy_name_map (dict[str, str]): 排名文件 stem → 中文显示名。
    rank_files (list[str]): 待处理排名文件名的完整列表（``trace_stock`` 逐个
        回看、report 逐个生成报告 JSON）。
    report_json_file_name_map (dict[str, str]): ``'{stem}_report.json'`` →
        中文名（report 取标题）。
"""
strategy_name_map = dict(
    concept_theme_strength_score='概念⋅板块',
    concept_stock_ma_cluster_breakout_strategy_score='概念⋅均线突破',
    concept_stock_lowend_startup_strategy_score='概念⋅低位启动',
    concept_stock_pullback_strategy_score='概念⋅回调',
    concept_stock_strength_strategy_score='概念⋅龙头强化',
    concept_stock_flow_strategy_score='概念⋅资金流',
    concept_stock_main_wave_strategy_score='概念⋅主升浪',
    industry_theme_strength_score='行业⋅板块',
    industry_stock_ma_cluster_breakout_strategy_score='行业⋅均线突破',
    industry_stock_lowend_startup_strategy_score='行业⋅低位启动',
    industry_stock_pullback_strategy_score='行业⋅回调',
    industry_stock_strength_strategy_score='行业⋅龙头强化',
    industry_stock_flow_strategy_score='行业⋅资金流',
    industry_stock_main_wave_strategy_score='行业⋅主升浪',
    stock_rise_rate_strategy_score='涨幅榜',
    stock_user_momentum_strategy_score='用户动量',
)

rank_files = [f'{name}.csv' for name in strategy_name_map.keys()]
report_json_file_name_map = dict([
    (f'{name}_report.json', cn_name)
    for name, cn_name in strategy_name_map.items()])
