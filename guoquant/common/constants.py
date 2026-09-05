"""
策略排名输出条数常量模块。

集中定义排名输出条数的顶层常量，用于控制主题/股票的读取、渲染与打印
条数（仅做头部截断，落盘的 CSV 始终全量写入）：

- ``THEME_TOP_LIMIT``：主题排名（``{type}_theme_strength_score.csv``）的
  头部截断数——rank 的板块分支只读取前 N 名主题参与选股、report 只渲染
  前 N 名主题（主题聚焦）；rank 写文件时控制台也仅打印前 N 行；
- ``STOCK_TOP_LIMIT``：股票排名 CSV 前 N 名的控制台打印条数，并作为
  ``trace_stock`` / ``report`` 的 ``--top`` 默认值（0 = 全部）。

被 ``guoquant/commands/`` 下的 ``rank.py`` / ``report.py`` /
``trace_stock.py`` / ``pipe_select_stocks.py`` 引用。
"""
THEME_TOP_LIMIT = 10
STOCK_TOP_LIMIT = 10
