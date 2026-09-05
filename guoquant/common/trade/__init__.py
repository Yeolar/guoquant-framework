"""
guoquant.common.trade 实盘自动交易子包。

与回测引擎（``guoquant.common.backtest``）对应，复用同一套策略注册与打分
机制，面向 QMT 实时行情与真实下单，被 ``commands/auto_trade.py`` （自动调仓）
与 ``commands/watch.py`` （看盘止损）引用。历史整表加载统一走
``common.quote.load_quote_dfs``，行业映射统一走 ``common.data_path`` 的
``load_industry_map``。

子模块按职责分层：

- ``data.py``：实盘数据——市值分层白名单股票池读取、行业映射再导出，QMT 当日
  实时快照（``fetch_latest_quote``）与“历史 + 当日快照”拼接构建 Quote
  （``build_all_quotes``）；
- ``scorer.py``：打分入口——从 ``common.strategy`` 注册表取引擎打分入口，对
  指定日期计算全市场得分（需 ``flow``/``dist`` 数据的策略在实盘拒绝）；
- ``strategy.py``：调仓计划——纯逻辑层：选股（top_n + 行业分散）、时间衰减
  追踪止损、调仓 / 看盘计划、人工选股文件读取；
- ``executor.py``：QMT 下单——通过 ``xtquant`` （``QmtExecutor``）连接 QMT
  客户端，查询账户 / 持仓、执行买卖下单；``fetch_current_prices`` 供实时取价。
"""
