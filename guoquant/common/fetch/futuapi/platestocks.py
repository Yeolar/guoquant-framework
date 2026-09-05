"""
板块成分股抓取（富途）——拉取单个板块的成分股并落盘 JSON。

职责：作为 futu provider 的一部分，由 commands/fetch_platestocks.py
调用（读取 fetch_platelist 产物后逐板块调用本函数）；产物由命令层写为
``{type}_plates/{code}_{name}.json`` （默认在 out/<日期>/fetched_data/
plate/ 下），供 guoquant.common.data_path.load_industry_map（行业板块
映射）与 guoquant.common.fetch.plates.fetch_stock_plates（个股板块反查，
需 records[].code）读取。

产物结构：{'plate': {'code', 'name'}, 'records': [...]}；records 由
DataFrame.to_dict('records') 得到，每行含 Futu 格式 stock_code
（如 SH.600000）、stock_name 等字段——这是 plates 反查与行业映射能命中
的前提（qmt provider 产物不含 Futu 格式代码列，见 qmtapi/platestocks.py）。
"""
import json
from guoquant.common.log import console
from futu import *


def fetch_plate_stocks(ctx, code, name, output):
    """抓取指定板块（code）的成分股并写 JSON。

    行情口径：调用 get_plate_stock(code) 取该板块全部成分股；成功时
    组装结果写入 output——records 为返回 DataFrame 的
    to_dict('records')，每行含 stock_code / stock_name / plate_code /
    plate_name 等字段（stock_code 为 Futu 格式，如 SH.600000）。
    失败（ret 非 RET_OK）时用 console.error 打印错误信息，不写文件。

    Args:
        ctx (futu.OpenQuoteContext): open_quote_context() 返回的行情
            上下文。
        code (str): 板块代码（如 ``BK0475``）。
        name (str): 板块名称；写入结果的 plate.name 字段，用于展示与
            命令层文件命名。
        output (str): 输出 JSON 文件路径。

    Returns:
        None: 无返回值；成功时结果直接写入 output 文件。
    """
    ret, data = ctx.get_plate_stock(code)
    if ret == RET_OK:
        result = {
            'plate': {
                'code': code,
                'name': name,
            },
            'records': data.to_dict('records'),
        }
        with open(output, 'w') as fp:
            json.dump(result, fp, ensure_ascii=False, indent=2)
    else:
        console.error(data)
