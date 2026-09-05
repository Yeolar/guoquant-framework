"""
板块列表抓取命令：抓取行业（industry）与概念（concept）两类板块列表并落盘 JSON。

实际抓取经 ``guoquant.common.fetch`` 依 .env 的 ``FETCH_PROVIDER`` 选择
provider：futu（默认，拉取富途行业/概念板块）或 qmt（从 xtdata 板块列表
按名称筛选）；板块列表的行结构随 provider 而异：futu 为
[代码, 名称, ...]，qmt 为单元素的 [板块名]。

产物路径（``<日期>/fetched_data/plate/`` 下，文件名定义见 ``plate_types``）：

- ``industry_plate_list.json``：行业板块列表；
- ``concept_plate_list.json``：概念板块列表。

依赖：``guoquant.common.fetch.fetch_plate_list`` （实际抓取）与
``plate_types`` （板块类型/文件名定义）。
下游：``fetch_platestocks`` 用本命令产出的列表抓取各板块成分股。
"""
import json
from datetime import date
import typer
from guoquant.common.log import console
from guoquant.common.utils import outp
from guoquant.common.fetch import *


def command(
    outputdir: str = typer.Option(
        '{d:%y%m%d}/fetched_data/plate',
        '-o',
        '--outputdir',
        help='output directory, default={d:%%y%%m%%d}/fetched_data/plate'),
) -> None:
    """按板块类型抓取板块列表（行业与概念各一个 JSON 文件）。

    对 ``plate_types`` 中每一项（industry / concept 与对应输出文件名）：

    - 目标文件已存在则打印提示并跳过（避免重复抓取）；
    - 否则在 ``open_quote_context()`` 行情上下文内调用
      ``fetch_plate_list`` 落盘。

    provider 分支：实际抓取在 ``guoquant.common.fetch.fetch_plate_list``
    内完成，按 .env 的 ``FETCH_PROVIDER`` 分发——futu 拉取富途行业/概念
    板块（行结构 [代码, 名称, ...]）；qmt 从 xtdata 板块列表按名称筛选
    （行结构 [板块名]）。两类列表文件均写入 outputdir。

    Args:
        outputdir (str): 输出目录模板，默认
            ``{d:%y%m%d}/fetched_data/plate``；``{d:%y%m%d}`` 用当天日期
            填充（同时创建该目录）。
    """
    outputdir = outp(outputdir.format(
        d=date.today()), is_dir=True)

    with open_quote_context() as ctx:
        # plate_types: [(板块类型, 输出文件名), ...]（industry/concept 各一）
        for type, file in plate_types:
            output = outputdir / file
            if output.exists():
                # 已存在则跳过，避免重复抓取
                console.print(f'already exist {output}', style='yellow bold')
                continue

            console.print(f'fetch plate list -> {output}')

            fetch_plate_list(ctx, type, output)
