"""
个股所属板块（本地文件反查）——futu get_owner_plate 的本地替代实现。

本模块是 provider 无关的本地反查：读取 commands/fetch_platestocks.py
（板块成分股抓取）产出的本地板块 JSON，反查个股所属行业/概念板块。
它不连接行情接口，也不依赖 futu-api / xtquant，因此不参与
fetch/__init__.py 的 provider 选择（无论 ``FETCH_PROVIDER`` 为何值都
固定可用），由各调用方以
``from guoquant.common.fetch.plates import fetch_stock_plates`` 引用。

数据来源：命令层落盘在 out/<日期>/fetched_data/plate/ 下的板块文件
（data_root 为 None 时自动取最新日期目录，见
guoquant.common.data_path.latest_data_root）：

- ``{type}_plates/{code}_{name}.json``：每板块一个成分股文件
  （fetch_platestocks 产出），结构为
  ``{'plate': {'code', 'name'}, 'records': [...]}``；本模块只取
  plate.name（板块名）与每条 record 的 code（成分股代码）做反查。
  fetch_platelist 产出的 ``{type}_plate_list.json`` 列表文件不参与读取。
- records[].code 需为 Futu 格式全代码（如 ``SH.600000``）才能命中待查
  codes：futu provider 的产物满足该结构；qmt provider 的成分股记录只有
  stock_code / stock_name（QMT 格式），不参与匹配，反查结果为空。

返回约定：``{code: [板块名, ...]}``，行业板块在前、概念板块在后
（与 futu 实时版 get_owner_plate 的排序约定一致）。
"""
import json
from collections import defaultdict
from pathlib import Path

from guoquant.common.data_path import latest_data_root


def fetch_stock_plates(codes, data_root=None):
    """读本地板块文件反查一批股票所属的行业/概念板块。

    反查口径：遍历 data_root 下 plate/{type}_plates/ 目录（type 依次为
    industry、concept）的每个 JSON，读板块名与成分股代码做集合匹配；
    单个文件损坏（json 解析失败）跳过、不影响整体（内部捕获 Exception
    静默 continue）。无板块文件目录（未跑 fetch_platestocks）或 codes
    为空时返回空映射。

    Args:
        codes (list[str]): 待查股票代码列表（Futu 格式，如
            ``SH.600000``；与成分股文件 records[].code 字段精确匹配，
            QMT 产物记录无法命中）。
        data_root (str, optional): 数据根目录（含 plate/ 子目录）；为
            None 时自动取 out/ 下最新日期的 fetched_data
            （guoquant.common.data_path.latest_data_root），无日期目录
            时同样返回空映射。

    Returns:
        dict[str, list[str]]: code → [板块名, ...]；行业板块插到列表最前，
        概念板块追加到末尾。
    """
    if data_root is None:
        data_root = latest_data_root()
    if data_root is None:
        return defaultdict(list)

    code_set = set(codes)
    code_plates = defaultdict(list)
    for type_ in ('industry', 'concept'):
        plate_dir = Path(data_root) / 'plate' / f'{type_}_plates'
        if not plate_dir.is_dir():
            continue
        for fpath in plate_dir.iterdir():
            if fpath.suffix != '.json':
                continue
            try:
                with open(fpath, encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                continue  # 单个板块文件损坏不影响整体
            plate_name = data.get('plate', {}).get('name', '')
            for rec in data.get('records', []):
                code = rec.get('code')
                if code in code_set:
                    if type_ == 'industry':
                        code_plates[code].insert(0, plate_name)
                    else:
                        code_plates[code].append(plate_name)
    return code_plates
