# guoquant 量化框架（guoziapp 工作区模板）

本目录是 `~/dev/guoziquant` 框架的精简重构版（backtrader 回测引擎 + typer 命令层，
**无 Django / manage.py**），由 guoziapp 创建工作区时自动复制。你只需实现**因子**与
**策略**，然后直接回测。

## 快速开始

```bash
# 1. 安装依赖（guoziapp 创建时会自动安装；手动安装用下面命令）
python -m pip install -r requirements.txt

# 2. 回测（示例数据已内置，可立即跑通）
python -m guoquant.cli backtest --strategy stock_strength_strategy --start 250101 --end 250630

# 3. 回测自定义策略（stock_user_momentum_strategy 为模板自带示例）
python -m guoquant.cli backtest --strategy stock_user_momentum_strategy --start 250101 --end 250630

# 4. 批量对比全部策略
python -m guoquant.cli backtest --benchmark --start 250101 --end 250630

# 5. 查看全部命令
python -m guoquant.cli --help
```

回测结果（收益/回撤/夏普/交易明细/净值图）输出到 `out/<日期>/backtest/`。

## 目录结构

```
guoquant/                     框架主包（所有代码统一在此，无 Django 风格 manage.py）
  cli.py                      typer 命令入口（python -m guoquant.cli <命令>）
  commands/                    typer 命令（backtest、panic_backtest、rank…）
  common/                     核心业务模块
    utils.py                  工具函数（BASE_DIR/outp/todate/keep_* 与策略数学工具）
    parallel.py               rich 控制台与并行工具
    quote.py                  行情 Quote 对象（因子/策略的输入载体）
    data_path.py / phase.py / report.py
    backtest/                 backtrader 回测引擎（runner/scorer/strategy）
    trade/                    QMT 实盘自动交易（可选，需 QMT 客户端）
    fetch/                    行情数据获取（futu-api / QMT，可选）
  factors/                    因子（@register_quote_factor 注册）
  strategies/                 策略（打分函数，_register_strategy 注册）
    user_strategies.py        ← 用户策略扩展点（示例）
out/<yyMMdd>/fetched_data/     行情数据（quote/flow/dist，JSON 或 parquet）
```

## 如何新增因子

1. 在 `guoquant/factors/` 新建文件（如 `user_my_factor.py`），用 `@register_quote_factor`
   装饰，签名 `factor(quote, window=None, ...)`（自动扫描，无需改其它文件）；
2. 策略里通过 `quote.<函数名>(window)` 调用；
3. 参照 `guoquant/factors/price_factors.py` 等现有因子模块。
   `quote` 可用数据：`close/high/low/volume/amount/turnover_rate/change_rate` 等。

## 如何新增策略

1. 在 `guoquant/strategies/` 新建文件（如 `user_my_strategy.py`），只定义
   **一个 quotes 级打分核心**并用装饰器注册（注册表见
   `guoquant/common/strategy.py`；文件由 strategies 包自动扫描导入，
   导入即完成注册，无需改框架文件）：
   ```python
   @register_strategy(name='my_strategy', need_mv=True)   # 引擎配置
   @window(60)                                            # 最少K线根数
   def stock_my_strategy_strategy(quotes):
       ...返回 (code, score) 降序序列...
   ```
   引擎配置参数：`needs_flow=True`（需要 flow+dist 数据）、
   `need_mv=True`（附带市值估算）、`has_theme=False`（不需要主题分）；
   注册名与函数名不一致时须显式传 `name`；
2. 打分核心输入：`quotes` 为 Quote 列表，元素个数与引擎配置对应
   （`need_mv=True` → `[Quote, market_value]` 对），返回按得分降序的
   `(code, score)` 序列；准入过滤在核心内做（`len < window` 由
   引擎/rank 外部完成）；
3. 回测：`python -m guoquant.cli backtest --strategy <策略名>`；
4. rank：按约定解析 `stock_<策略名>_strategy(quotes)` 直接调用同一打分
   核心——无需板块/市值元数据的策略须加入 `commands/rank.py` 的
   `PLAIN_STRATEGIES` 走无板块分支，参照内置策略（`stock_strength_strategy.py`、
   `stock_rise_rate_strategy.py`）与扩展点 `user_strategies.py`。

## 内置策略与命令

策略（打分核心函数全名即策略名）：`stock_strength_strategy`（个股强度）、
`stock_flow_strategy`（资金流，需 flow+dist 数据）、`stock_main_wave_strategy`、
`stock_lowend_startup_strategy`、`stock_pullback_strategy`、
`stock_ma_cluster_breakout_strategy`、`stock_rise_rate_strategy`。


常用命令（统一 `python -m guoquant.cli <命令>`）：

| 命令 | 用途 |
|---|---|
| `backtest` | 通用回测（`--strategy/--start/--end/--top-n/--rebalance/--benchmark`） |
| `rank` | 股票/主题打分排名（`--strategy stock_strength_strategy/theme_strength/…`，函数全名即策略名，新增策略自动可用） |
| `panic_backtest` | 极端恐慌逆向策略回测（`--hold/--sweep` 参数扫描） |
| `check_market_sentiment_cycle` | 市场情绪周期检查 |
| `ic_analysis` | 因子 IC 分析 |

## 真实数据（可选）

模板自带 `out/` 下约半年、5 只股票的**示例数据**（仅用于验证链路与基线回测，
结果无投资意义）。真实回测请先抓取行情：

```bash
# 行情抓取 provider 由 .env 指定（FETCH_PROVIDER=futu 默认 / =qmt），
# 复制 .env.example 为 .env 后按需修改。
# 富途（默认）：需本地 FutuOpenD + 富途行情权限（requirements 已含 futu-api）
python -m guoquant.cli fetch_stockdata -f <股票列表.csv>          # 日/周 K 行情
python -m guoquant.cli fetch_stockcapitalflow -f <列表.csv>       # 资金流
python -m guoquant.cli fetch_stockcapitaldist -f <列表.csv>       # 筹码分布
```

- 股票列表 CSV 格式：每行 `代码,名称`（如 `SH.600000,浦发银行`）；
- 数据写入 `out/<yyMMdd>/fetched_data/`，目录约定：
  ```
  quote/{d|w}/stock/{code}.json|parquet   # K线（stock/ 与 index/ 平行）
  quote/{d|w}/index/SH.LIST*.parquet      # 指数K线
  flow/{d|w}/{code}.json|parquet          # 资金流向（无子目录）
  dist/{code}.json|parquet                # 筹码分布快照（不分周期）
  ```
  JSON 为 records 列表、`t` 为 unix 秒，字段 `o/h/l/c/v/a/tr/pe/lc`；日频至少 120 行；
- 股票池（按市值分层保留）：`out/<yyMMdd>/stockfiltered/stock_mv_{10|30|100|300}.csv`；
- 建议同时提供 `quote/d/index/SH.000300.parquet`（沪深300，用于市场择时）
  与 `plate/industry_plates/*.json`（行业分类）。

## 实盘交易（可选，风险自担）

`guoquant/common/trade/`（QMT 实盘执行）与 `watch`/`auto_trade` 命令依赖国金 QMT 客户端
（xtquant 随客户端分发，需将安装目录加入 PYTHONPATH）：

```bash
# 模拟运行（仅计算，不真实下单）
python -m guoquant.cli auto_trade --strategy stock_strength_strategy --root 250630/fetched_data --dry-run
# 实盘（谨慎！）需先启动并登录 QMT
python -m guoquant.cli auto_trade --strategy stock_strength_strategy --root 250630/fetched_data --mini-path <QMT路径> --account <账号>
```

**注意**：agent 默认不会执行实盘交易命令；仅在明确要求且权限允许时才可运行。

## 自动研究（Autoresearch）

框架与 guoziapp 的 DSH agent 配合可做自动化因子/策略迭代研究。**研究纪律**
（agent 与人都应遵守，避免过拟合与无效迭代）：

1. **基线先行**：每轮迭代前先跑 `backtest --benchmark`（或对照内置 `strength`），
   只有明确优于基线才保留改动；
2. **样本外纪律**：调参只用数据的前 80%，最后约 60 个交易日**仅用于验证**
   （`--end` 前移做样本内、全区间做最终确认），绝不把验证段纳入调参；
3. **只动扩展点**：只新增/修改 `guoquant/factors/user_*.py` 与
   `guoquant/strategies/user_*.py`，框架文件受写保护；
4. **多指标联合评估**：不只看总收益 —— Sharpe、最大回撤、交易笔数
   （过少=噪声、过多=交易成本）一起看；因子用 `ic_analysis` 验证预测力；
5. **留痕**：每轮改动 `git commit`，指标记入研究日志
   （guoziapp 自动研究模式写入 `research/log.jsonl`），便于回滚与审计；
6. **停止条件**：连续多轮无改善即停止，避免在噪声里无限迭代。

guoziapp 量化页的「自动研究」入口按上述纪律编排多轮迭代：每轮 agent 提议并
实现改进 → 自动回测全部策略 → 指标写入研究日志 → 达标或轮次耗尽后给出报告。

## 与原始框架的差异（本模板改造点）

1. **去掉 Django / manage.py**：命令层改为 typer（`guoquant/cli.py` 自动发现
   `guoquant/commands/` 下导出 `command` 函数的模块）；
2. **目录重构**：原 `guozi/` 与 `quote/` 合并为单一 `guoquant/` 包，
   命令从 `quote/management/commands/` 移到 `guoquant/commands/`；
3. 裁掉依赖外部上传服务的 `report` 命令（`guoquant/common/name_map.py` 模块保留）；
4. `guoquant/factors/__init__.py` 增加自动扫描（用户因子免改注册文件）；
5. 策略注册表统一到 `guoquant/common/strategy.py`，内置与用户策略都在各自
   `guoquant/strategies/*.py` 模块内用 `@register_strategy` 装饰器自注册
   （导入 `guoquant.strategies` 即注册全部策略，`--strategy` 按注册表动态校验）；
6. `backtest` 命令的 `--strategy` 改为按注册表动态校验（新增策略立即可用）；
7. `guoquant/common/utils.py` 移除 `cityhash` 等外部服务相关依赖；
8. `guoquant/common/fetch/__init__.py` 与 `qmtapi/context.py`、`guoquant/common/trade/*` 将
   futu-api / xtquant 改为可选依赖（未安装不阻塞回测主链路）；
9. 新增 `requirements.txt`、用户扩展点示例与示例数据（不依赖 settings/.env）。

升级框架时若与上述文件冲突，以模板行为为准（其余文件可从原框架同步）。
