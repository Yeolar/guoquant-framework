guoquant 量化框架文档
=====================

guoquant 是 guoziapp 工作区（quant 页面）内置的量化研究框架：无 Django
依赖，typer 命令行驱动，数据、因子、策略、回测与实盘一体化。

快速上手
--------

.. code-block:: bash

    pip install -r requirements.txt
    python -m guoquant.cli --help

主要命令一览
------------

.. list-table::
   :widths: 30 70
   :header-rows: 1

   * - 命令
     - 说明
   * - ``backtest``
     - 回测：按注册表策略名运行历史模拟（--strategy stock_strength_strategy / …）
   * - ``rank``
     - 当日选股排名：--strategy 指定策略名（strength / main_wave / …，解析到
       stock_<名>_strategy 打分核心；theme_strength 输出主题排名，rise_rate
       为无板块分支），--type industry/concept 控制板块维度
   * - ``check_market_sentiment_cycle``
     - 市场情绪周期检测，输出 E/M 到 market_sentiment_cycle.txt
   * - ``fetch_*`` / ``pipe_*``
     - 行情/资金流/筹码/市值白名单等数据抓取与流水线
   * - ``report``
     - 排名 CSV → 报告 JSON（板块/股票两类，含个股说明与历史追踪）
   * - ``trace_stock`` / ``watch``
     - 个股跟踪与监控
   * - ``ic_analysis`` / ``panic_backtest``
     - IC 分析与极端恐慌逆向回测
   * - ``auto_trade``
     - QMT 实盘自动交易（可选，需 QMT 客户端）

框架要点
--------

- **数据目录约定**：fetched_data/ 下 ``quote/d|w/stock|index`` K线、
  ``flow/d|w`` 资金流、``dist`` 筹码快照、``plate`` 板块、``stockfiltered``
  市值白名单。
- **因子注册**：``guoquant/factors/`` 内 ``@register_quote_factor`` 装饰器
  注册，经 ``quote.因子名(...)`` 惰性调用，见 ``common/quote.py``。
- **策略两层约定**：``strategies/*.py`` 内 ``@register_strategy`` 注册引擎
  打分入口（DataFrame → {code: score}），``stock_<策略名>_strategy(quotes)``
  为 rank/回测共用的打分核心，最少K线根数用 ``@window(n)`` 声明。
- **命令自动发现**：``commands/`` 下导出 ``command`` 函数的模块即一个
  typer 子命令。

.. toctree::
   :maxdepth: 6
   :caption: API 参考

   api/modules
