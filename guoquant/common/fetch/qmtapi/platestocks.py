"""
板块成分股抓取（QMT）——按板块名取成分股并落盘 JSON。

职责：作为 qmt provider 的一部分，由 commands/fetch_platestocks.py
调用（qmt 模式下按板块名逐板块抓取）；产物由命令层写为
``{type}_plates/{code}_{name}.json`` （默认在 out/<日期>/fetched_data/
plate/ 下）。

产物结构：{'plate': {'code', 'name'}, 'records': [...]}。QMT 以板块名
（而非代码）标识板块，板块名同时写入 plate.code / plate.name；records
每行为 {'stock_code': QMT 代码（如 600000.SH）, 'stock_name': 名称}，
不含 Futu 格式代码列——因此该产物不参与 fetch/plates.py（个股板块反查，
需 records[].code）与 data_path.load_industry_map（行业映射）的匹配。
"""
import json
from guoquant.common.log import console
from xtquant import xtdata


def fetch_plate_stocks(ctx, code, name, output):
    """抓取指定板块（以 name 标识）的成分股，附上名称后写 JSON。

    抓取口径：以板块名调用 get_stock_list_in_sector(name) 取成分股
    （QMT 代码，如 600000.SH），再经 get_instrument_detail_list 批量取
    每只的名称，组装 records（{'stock_code', 'stock_name'}）与 plate
    （code/name 均填板块名）写入 output。板块名不存在等异常时
    console.error 打印错误并返回，不写文件。

    Args:
        ctx: 与 futu 版签名对齐的参数（xtdata 为模块级调用，未使用）。
        code (str): 板块代码；与 futu 版对齐的占位参数——QMT 无独立板块
            代码，实际以 name 为准，本参数未使用。
        name (str): 板块名（get_stock_list_in_sector 的板块标识）；写入
            plate.code / plate.name，命令层还用于文件命名。
        output (str): 输出 JSON 文件路径。

    Returns:
        None: 无返回值；成功时结果直接写入 output 文件。
    """
    try:
        stocks = xtdata.get_stock_list_in_sector(name)
    except Exception as e:
        console.error(f'{e}')
        return

    info = xtdata.get_instrument_detail_list(stocks)
    records = []
    for s in stocks:
        sname = info.get(s, {}).get('InstrumentName', s)
        records.append({'stock_code': s, 'stock_name': sname})

    result = {
        'plate': {
            'code': name,
            'name': name,
        },
        'records': records,
    }
    with open(output, 'w') as fp:
        json.dump(result, fp, ensure_ascii=False, indent=2)
