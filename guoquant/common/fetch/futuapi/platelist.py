"""
板块列表抓取（富途）——拉取行业/概念板块列表并落盘 JSON。

职责：作为 futu provider 的一部分，由 commands/fetch_platelist.py 调用
（qmt provider 复用同一命令入口，见 qmtapi/platelist.py）；产物为
``{type}_plate_list.json``，由命令层落盘在 out/<日期>/fetched_data/plate/
下，是 commands/fetch_platestocks.py 的输入（逐板块抓成分股）。

行结构约定：列表每个元素是一行 [code, name, ...]（get_plate_list 返回
DataFrame 的 values.tolist()，通常 [板块代码, 板块名称]）；下游
fetch_platestocks 命令按 ``code, name = plate[:2]`` 解包，与 qmt
provider 的单元素行 [板块名] 不兼容。

模块级常量：plate_types 定义板块类型与输出文件名的对应（见文件内注释），
fetch_platelist 命令据此遍历。
"""
import json
from guoquant.common.log import console
from futu import *


# 要抓取的板块类型 → 输出文件名：
#   INDUSTRY 行业板块 → industry_plate_list.json
#   CONCEPT  概念板块 → concept_plate_list.json
plate_types = [
    [Plate.INDUSTRY, 'industry_plate_list.json'],
    [Plate.CONCEPT, 'concept_plate_list.json'],
]

def fetch_plate_list(ctx, type, output):
    """抓取指定类型（Plate.INDUSTRY/CONCEPT）的板块列表并写 JSON。

    行情口径：请求 Market.SH 下该类型的全部板块；成功后把返回 DataFrame
    的 values.tolist() 整表写入 output（每行 [code, name, ...]）。
    失败（ret 非 RET_OK）时仅用 console.error 打印错误信息：不写文件、
    不抛异常，也没有返回值可供上层区分成败。

    Args:
        ctx (futu.OpenQuoteContext): open_quote_context() 返回的行情
            上下文。
        type (futu.Plate): 板块类型枚举，取值见模块级 plate_types
            （Plate.INDUSTRY / Plate.CONCEPT）。
        output (str): 输出 JSON 文件路径；内容为列表，元素是
            [code, name, ...] 形式的板块行。

    Returns:
        None: 无返回值；成功时结果直接写入 output 文件。
    """
    ret, data = ctx.get_plate_list(Market.SH, type)
    if ret == RET_OK:
        result = data.values.tolist()
        with open(output, 'w') as fp:
            json.dump(result, fp, ensure_ascii=False, indent=2)
    else:
        console.error(data)
