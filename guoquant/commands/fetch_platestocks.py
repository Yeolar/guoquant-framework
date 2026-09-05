"""
板块成分股抓取命令：读取板块列表 JSON，逐板块抓取成分股并落盘 JSON。

输入为 ``fetch_platelist`` 产出的板块列表（``{type}_plate_list.json``），
对 industry / concept 两类各执行一遍；每个板块一个成分股文件，
板块之间 sleep 3 秒限速（避免触发行情接口频率限制）。

产物路径：``<日期>/fetched_data/plate/{type}_plates/{code}_{name}.json``
（名称中的 '/' 替换为 '-' 以兼容文件名；文件结构为
{'plate': {'code', 'name'}, 'records': [...]}）。

依赖：``guoquant.common.fetch.fetch_plate_stocks`` （实际抓取）。
下游：rank 的板块/主题分支读取 ``plate/{type}_plates/`` 目录做选股。
"""
import json
import time
from datetime import date
from pathlib import Path
import typer
from rich.progress import track
from guoquant.common.log import console
from guoquant.common.utils import outp
from guoquant.common.fetch import *


def command(
    file: str = typer.Option(
        '{d:%y%m%d}/fetched_data/plate/{type}_plate_list.json',
        '-f',
        '--file',
        help='source file, default={d:%%y%%m%%d}/fetched_data/plate/{type}_plate_list.json'),
    output: str = typer.Option(
        '{d:%y%m%d}/fetched_data/plate/{type}_plates/{code}_{name}.json',
        '-o',
        '--output',
        help='output files, default={d:%%y%%m%%d}/fetched_data/plate/{type}_plates/{code}_{name}.json'),
) -> None:
    """按板块类型逐板块抓取成分股（industry / concept 各跑一遍）。

    对每个类型读取一份板块列表 JSON，在 ``open_quote_context()`` 共享
    行情上下文内逐个板块抓取成分股，写入对应 ``{type}_plates/`` 输出
    （板块之间 sleep 3 秒限速；进度由 rich track 显示）。

    provider 分支：实际抓取在 ``fetch_plate_stocks`` 内完成，按 .env
    的 ``FETCH_PROVIDER`` 分发——futu 返回含 ``stock_code`` /
    ``stock_name`` / ``plate_code`` / ``plate_name`` 等字段的记录；
    qmt 以板块名取成分股，records 仅含 ``stock_code``/``stock_name``
    （QMT 格式代码，不参与个股板块本地反查）。

    Args:
        file (str): 板块列表源文件模板，默认
            ``{d:%y%m%d}/fetched_data/plate/{type}_plate_list.json``；
            ``{d}`` 填当天日期、``{type}`` 由循环填 industry/concept。
        output (str): 输出文件路径模板，默认
            ``{d:%y%m%d}/fetched_data/plate/{type}_plates/{code}_{name}.json``；
            ``{code}``/``{name}`` 占位由 fetch() 按板块填充。
    """
    for type in ['industry', 'concept']:
        source = outp(file.format(d=date.today(), type=type))
        with open(source) as fp:
            plates = json.load(fp)

        with open_quote_context() as ctx:
            # 逐板块抓取，track 显示进度
            for plate in track(plates, console=console):
                fetch(ctx, plate, type, output)


def fetch(ctx, plate, type, output_opt):
    """抓取单个板块的成分股并写 JSON 文件（目标文件已存在则跳过）。

    调用方负责板块间的 sleep 限速。

    Args:
        ctx: ``open_quote_context()`` 返回的行情上下文。
        plate (list): 板块记录（[代码, 名称, ...]，取前两项使用）。
        type (str): 板块类型（industry / concept），用于填充输出路径。
        output_opt (str): 输出文件路径模板，含 ``{d}``/``{type}``/``{code}``/
            ``{name}`` 占位；板块名称中的 '/' 会替换为 '-'。
    """
    # plate 为 [代码, 名称, ...] 结构；名称中的 '/' 替换为 '-' 以兼容文件名
    code, name = plate[:2]
    output = outp(output_opt.format(d=date.today(),
                                    type=type,
                                    code=code,
                                    name=name.replace('/', '-')))
    if output.exists():
        console.print(f'already exists {output}', style='yellow bold')
        return

    console.print(f'get stocks for plate {name} -> {output}')

    fetch_plate_stocks(ctx, code, name, output)
    # 限速：板块间间隔 3 秒，避免触发行情接口频率限制
    time.sleep(3)
