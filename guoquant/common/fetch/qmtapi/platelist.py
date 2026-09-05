"""
板块列表抓取（QMT）——从 xtdata.get_sector_list() 筛选行业/概念板块并落盘 JSON。

职责：作为 qmt provider 的一部分，由 commands/fetch_platelist.py 调用
（与 futu provider 同一命令入口）；产物为 ``{type}_plate_list.json``，
由命令层落盘在 out/<日期>/fetched_data/plate/ 下，名义上是
commands/fetch_platestocks.py 的输入。

已知缺陷（与 futu 产物不兼容）：QMT 板块无独立代码，板块名即唯一标识，
本模块把每行写成单元素列表 [板块名]；而 futu 版每行是 [code, name, ...]
（见 futuapi/platelist.py）。共享命令 commands/fetch_platestocks.py 里
按 futu 行结构执行 ``code, name = plate[:2]`` 解包，QMT 的单元素行会抛
ValueError，因此该列表目前无法直接喂给现版 fetch_platestocks 命令
（需配套行格式或命令层分支改造）。

其他现状：本模块导入了 console 但未使用（无错误处理分支）；
get_sector_list 抛出的异常会直接上抛，不会写成失败标记。
"""
import json
from guoquant.common.log import console
from xtquant import xtdata


# 要抓取的板块类型 → 输出文件名：industry / concept 各一
plate_types = [
    ['industry', 'industry_plate_list.json'],
    ['concept', 'concept_plate_list.json'],
]


def fetch_plate_list(ctx, ptype, output):
    """抓取指定类型（industry/concept）的板块列表，按名称前缀筛选落盘。

    筛选口径：xtdata.get_sector_list() 返回板块名列表；ptype 为
    'industry' 时取以 'SW1' 开头且名称不含 '加权' 的申万一级板块，
    'concept' 时取 'GN' 前缀的概念板块，其他值则按该前缀直接匹配板块名。
    结果保留匹配到的原始板块名（含前缀），每行写成单元素列表
    [板块名] 后 json.dump 到 output；无匹配时写出空列表（仍会生成文件）。

    已知缺陷：单元素行 [板块名] 与 futu 产物的 [code, name, ...] 行
    结构不一致，无法被共享命令 commands/fetch_platestocks.py 按
    ``code, name = plate[:2]`` 解包（详见本模块 docstring）。

    Args:
        ctx: 与 futu 版签名对齐的参数（xtdata 为模块级调用，未使用）。
        ptype (str): 板块类型：'industry' 或 'concept'，或任意板块名前缀。
        output (str): 输出 JSON 文件路径。

    Returns:
        None: 无返回值；结果直接写入 output 文件。
    """
    sector_list = xtdata.get_sector_list()
    if ptype == 'industry':
        names = [s for s in sector_list
                 if s.startswith('SW1') and '加权' not in s]
    elif ptype == 'concept':
        names = [s for s in sector_list if s.startswith('GN')]
    else:
        names = [s for s in sector_list if s.startswith(ptype)]

    result = [[n] for n in names]
    with open(output, 'w') as fp:
        json.dump(result, fp, ensure_ascii=False, indent=2)
