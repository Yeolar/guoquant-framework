"""
guoquant.common.fetch — 行情数据抓取的统一入口（provider 由 .env 选择）。

本包把富途（futu-api）与国金 QMT（xtquant）两套行情抓取封装成同一套
函数签名，再按 ``.env`` 的 ``FETCH_PROVIDER`` 选定 provider 并统一
re-export：``commands/fetch_*.py`` 通过 ``from guoquant.common.fetch import *`` 使用，
命令代码不感知 provider。抓取产物由命令层落盘到 ``out/<日期>/fetched_data/`` 下，
回测主链路不依赖本包。

对外导出的名字（随 provider 指向 futuapi/ 或 qmtapi/ 子包的实现）：

- ``open_quote_context``：行情连接上下文（contextmanager，见两 provider
  的 context.py）
- ``plate_types``：板块类型 → 输出文件名（fetch_platelist 命令遍历用）
- ``fetch_plate_list``：板块列表（行业/概念，plate/ 下 JSON）
- ``fetch_plate_stocks``：板块成分股（plate/{type}_plates/ 下 JSON）
- ``fetch_stock_filtered``：股票筛选（按流通市值区间，CSV）
- ``fetch_stock_quote``：K 线行情（quote/{周期}/stock/ 下 parquet）
- ``fetch_stock_capital_dist``：筹码分布（dist/ 下 parquet）
- ``fetch_stock_capital_flow``：资金流（flow/{周期}/ 下 parquet）
- ``fetch_stock_plates``：个股所属板块（本地文件反查，provider 无关，
  见 plates 模块，固定导出）

provider 选择（``.env`` 配置 ``FETCH_PROVIDER``）：

- ``futu``：默认。富途 OpenAPI，需本机运行富途 OpenD（默认端口 11111）
  并安装 futu-api 包。
- ``qmt``：国金 QMT，需运行 QMT 客户端；xtquant 随客户端分发。

约定（guoziapp 模板改造）：futu-api / xtquant 为可选依赖。导入时若所选
provider 依赖缺失，此处捕获 ImportError 并跳过导出，保证回测主链路
（不依赖 fetch）始终可用；依赖缺失要到对应命令运行到抓取环节才暴露。
注意 provider 内部仍存在未导入名字即被引用的缺陷（例如 qmtapi 的
stockcapitaldist / stockcapitalflow 占位实现引用未导入的 console，
调用会抛 NameError），详见各文件 docstring。
"""
import os

from dotenv import load_dotenv

# 命令入口（cli.py）已加载 .env；此处再加载一次保证被直接 import 时也生效
load_dotenv('.env')

_PROVIDER = os.environ.get('FETCH_PROVIDER', 'futu').strip().lower()

try:
    if _PROVIDER == 'qmt':
        # QMT provider：xtquant 依赖随客户端分发，缺失时捕获导入错误
        from .qmtapi.context import open_quote_context
        from .qmtapi.platelist import fetch_plate_list, plate_types
        from .qmtapi.platestocks import fetch_plate_stocks
        from .qmtapi.stockfiltered import fetch_stock_filtered
        from .qmtapi.stockquote import fetch_stock_quote
        from .qmtapi.stockcapitaldist import fetch_stock_capital_dist
        from .qmtapi.stockcapitalflow import fetch_stock_capital_flow
    else:
        # 默认 futu provider
        from .futuapi.context import open_quote_context
        from .futuapi.platelist import fetch_plate_list, plate_types
        from .futuapi.platestocks import fetch_plate_stocks
        from .futuapi.stockfiltered import fetch_stock_filtered
        from .futuapi.stockquote import fetch_stock_quote
        from .futuapi.stockcapitaldist import fetch_stock_capital_dist
        from .futuapi.stockcapitalflow import fetch_stock_capital_flow
except ImportError:
    # 所选 provider 的依赖未安装：跳过导入，命令运行到抓取环节时
    # 会以 NameError/ImportError 提示；回测主链路不受影响。
    pass

# 个股所属板块（本地文件反查）：provider 无关，读 fetch_platestocks
# 产出的本地板块 JSON，无需连接行情接口，也不依赖 futu-api / xtquant
# —— 因此放在 provider 选择之外。
from .plates import fetch_stock_plates  # noqa: E402
