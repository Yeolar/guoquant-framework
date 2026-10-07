"""
板块指数清单重写命令：合并行业/概念板块列表并重写为“过滤格式”CSV。

读取 industry / concept 两个板块列表 JSON（``{type}_plate_list.json``），
合并后重写为每行 ``代码, 名称`` 的 CSV，并在开头固定写入
上证指数 / 深证成指 / 沪深300 / 中证1000 四个基准指数。

输出文件 ``<日期>/plate_index.csv``，在
``pipe_fetch_platestocks_and_stockdata`` 中作为 fetch_stockquote 的清单
输入：把四个基准指数与全部板块代码都当作行情标的抓取 K 线
（落盘于 ``quote/<周期>/index/``）。
"""
import json
from datetime import date
import typer
from guoquant.common.log import console
from guoquant.common.utils import outp


def command(
    file: str = typer.Option(
        '{d:%y%m%d}/fetched_data/plate/{type}_plate_list.json',
        '-f',
        '--file',
        help='source file, default={d:%%y%%m%%d}/fetched_data/plate/{type}_plate_list.json'),
    output: str = typer.Option(
        '{d:%y%m%d}/plate_index.csv',
        '-o',
        '--output',
        help='output file, default={d:%%y%%m%%d}/plate_index.csv'),
) -> None:
    """合并行业 + 概念板块列表，重写为“过滤格式”的 ``plate_index.csv``。

    先合并 industry / concept 两个板块列表，再在输出开头固定写入四个
    基准指数（上证指数 / 深证成指 / 沪深300 / 中证1000），其后为全部
    板块行（每行 ``代码, 名称``）；输出已存在则跳过（避免重复覆盖）。

    板块列表行结构依赖 fetch_platelist 的 futu 产物 [代码, 名称, ...]；
    qmt 产物为单元素的 [板块名] 行，不含名称列，本命令在 qmt provider
    下会下标越界，不适用。

    Args:
        file (str): 板块列表源文件模板，默认
            ``{d:%y%m%d}/fetched_data/plate/{type}_plate_list.json``；
            ``{d}`` 填当天日期、``{type}`` 由循环填 industry/concept。
        output (str): 输出 CSV 文件模板，默认 ``{d:%y%m%d}/plate_index.csv``。
    """
    j = []
    for type in ['industry', 'concept']:
        # {type} 模板由本循环填充，{d} 固定用今天日期
        source = outp(file.format(d=date.today(), type=type))
        with open(source) as fp:
            plates = json.load(fp)
            # plate_list.json 为 [代码, 名称, ...] 结构的列表
            j.extend(plates)

    output = outp(output.format(d=date.today()))
    if output.exists():
        # 已存在则跳过，避免重复生成覆盖
        console.print(f'already exist {output}', style='yellow bold')
        return

    console.print(f'rewrite -> {output}')

    with open(output, 'w') as fp:
        # 前四行固定为常用宽基指数，之后为全部行业/概念板块
        fp.write('SH.000001, 上证指数\n')
        fp.write('SZ.399001, 深证成指\n')
        fp.write('SH.000300, 沪深300\n')
        fp.write('SH.000852, 中证1000\n')
        for item in j:
            fp.write(f'{item[0]}, {item[1]}\n')
