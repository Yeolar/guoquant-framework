"""guoquant.common：共享基础层（与具体行情 provider 解耦）。

集中放置跨 ``commands`` / ``factors`` / ``strategies`` 共用的实现，按职责
拆成模块与子包；各模块详细职责见对应模块 docstring。

按职责分为四类：

- 通用工具：``utils.py`` （``BASE_DIR`` / ``outp`` / ``todate`` / 限速）、
  ``log.py`` （统一日志出口）、``parallel.py`` （并发执行与进度条）、
  ``sync.py`` （远端 rsync 同步）；
- 数据与行情：``data_path.py`` （目录约定与文件路径枚举）、``quote.py``
  （``Quote`` 封装、因子注册、文件读取 / 整表加载与组装）、``scoring.py``
  （打分执行辅助）、``phase.py`` （情绪阶段判定）、``constants.py``
  （输出条数常量）、``name_map.py`` （排名产物命名映射）；
- 策略机制：``strategy.py`` （``Strategy`` 基类、``@window``、
  ``@register_strategy`` 注册表）、``theme.py`` （``Theme`` 容器与主题
  聚合因子注册）；
- 子包：``fetch/`` （富途 / QMT 行情抓取）、``backtest/`` （回测引擎）、
  ``trade/`` （实盘数据与执行），三者复用上述模块。
"""
