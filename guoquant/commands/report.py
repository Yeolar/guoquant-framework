"""report 命令：把排名 CSV 整理为报告 JSON（供前端 HTML 渲染）。

输出 ``{root}/{排名文件名}_report.json``：

- theme 排名 → ``type=plate`` 的板块列表；
- 股票排名 → ``type=stock`` 的股票列表（含行业/概念板块、个股说明 markdown、
  流通市值、历史追踪）。
"""
import json
from datetime import date
from pathlib import Path

import markdown
import typer

from guoquant.common.data_path import filtered_stock_paths
from guoquant.common.log import console
from guoquant.common.name_map import report_json_file_name_map, strategy_name_map
from guoquant.common.utils import outp, todate
from guoquant.common.constants import STOCK_TOP_LIMIT, THEME_TOP_LIMIT

# 概念板块过滤集：从成分股概念板块中剔除，避免报告里堆砌噪声概念
attr0_concept_plates = set("""
    沪股通 深股通 AH股
    融资融券 转融券标的
    科创企业同股同权 股权激励 股权转让
    """.split())
attr1_concept_plates = set("""
    昨日首板
    龙头股 白马股
    MSCI概念 纳入富时罗素
    摘帽 业绩反转
    """.split())


class Item(dict):
    """键值即属性的字典子类（``item.code`` / ``item.score`` 式访问）。

    Attributes:
        无固定属性模式：``__getattr__`` / ``__setattr__`` 把任意属性读写映射
        为字典键。下游按需设置条目字段，如 ``code`` / ``name`` / ``score`` /
        ``plate``、板块字段（``plate_industry`` / ``plate_concept`` /
        ``plate_attr0_concept`` / ``plate_attr1_concept``）、``info`` （个股说明
        markdown 渲染后的 HTML）、``market_value`` （流通市值，亿元）与
        ``traces`` （历史上榜记录）。
    """

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        self[name] = value


def command(
    file: str = typer.Option(
        ...,
        '-f',
        '--file',
        help='rank result csv file'),
    top: int = typer.Option(
        STOCK_TOP_LIMIT,
        '--top',
        help=f'top count, default={STOCK_TOP_LIMIT} (0=all)'),
    root: str = typer.Option(
        '{d:%y%m%d}/render',
        '--root',
        help='root directory, default={d:%%y%%m%%d}/render'),
    date: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--date',
        help=f'date, default={date.today():%y%m%d}'),
) -> None:
    """把排名 CSV 整理为报告 JSON，写入 ``{root}/{排名文件名}_report.json``。

    按来源文件名分流：theme 排名（``*_theme_strength_score.csv``）输出
    ``type=plate`` 的板块列表（取前 ``THEME_TOP_LIMIT`` 名）；其余股票排名输出
    ``type=stock`` 的股票列表，并按 ``top`` 截取前若干名，同时附带：

    - 所属行业/概念板块：读本地板块文件反查（``fetch_platelist`` +
      ``fetch_platestocks`` 产物，无需连接行情接口），并把噪声概念按
      ``attr0_concept_plates`` / ``attr1_concept_plates`` 过滤集分类；
    - 个股说明：扫描 ``root`` 下 ``<代码>.info.md``，经 markdown 渲染为 HTML；
    - 流通市值：取自 filtered 股票 CSV，单位换算为亿元；
    - 历史追踪：读取 ``rank/trace.json``，策略名经 ``strategy_name_map`` 映射。
    报告标题取 ``report_json_file_name_map`` 对输出文件名的映射。

    Args:
        file (str): rank 结果 CSV 文件路径。
        top (int): 股票排名截取条数，default=``STOCK_TOP_LIMIT`` （0 表示全部）。
        root (str): 输出根目录，default=``{d:%y%m%d}/render``。
        date (str): 用于 ``root`` 模板与数据目录定位的日期（YYMMDD），
            default=今天。
    """
    d = todate(date)
    source = Path(file)
    type = source.stem.split('_')[0]
    root = outp(root.format(d=d), is_dir=True)
    output = root / f'{source.stem}_report.json'

    console.print(f'render output -> {output}')

    plates = []
    plate_map = {}
    stocks = []
    stock_map = {}

    title = report_json_file_name_map[output.name]

    if source.name in """
            concept_theme_strength_score.csv
            industry_theme_strength_score.csv
            """.split():
        with open(source) as fp:
            for line in fp.readlines()[:THEME_TOP_LIMIT]:
                parts = [i.strip() for i in line.split(',')]
                item = Item()
                item.code = parts[0]
                item.name = parts[1]
                item.score = float(parts[2])
                plates.append(item.code)
                plate_map[item.code] = item

        plates = [plate_map[code] for code in plates]

        with open(output, 'w') as fp:
            json.dump({
                'title': title,
                'type': 'plate',
                'stocks': plates,
            }, fp)

    else:
        with open(source) as fp:
            lines = fp.readlines()
            if top > 0:
                lines = lines[:top]
            for line in lines:
                parts = [i.strip() for i in line.split(',')]
                item = Item()
                item.code = parts[0]
                item.name = parts[1]
                if len(parts) == 4:
                    item.plate = parts[2]
                    item.score = float(parts[3])
                else:
                    item.plate = ''
                    item.score = float(parts[2])
                stocks.append(item.code)
                stock_map[item.code] = item

        # 个股所属板块：读本地板块文件反查（fetch_platelist +
        # fetch_platestocks 产物），无需连接行情接口
        from guoquant.common.fetch import fetch_stock_plates
        data_root = outp(f'{d:%y%m%d}/fetched_data')
        stock_plates = fetch_stock_plates(stocks, data_root)
        for code in stock_map.keys():
            plate_industry = stock_plates[code][0]
            stock_map[code].plate_industry = plate_industry
            if stock_map[code].plate == plate_industry:
                stock_map[code].plate = ''
            concept_plates = sorted(stock_plates[code][1:])
            stock_map[code].plate_concept = [
                plate for plate in concept_plates
                if plate not in attr0_concept_plates and
                plate not in attr1_concept_plates]
            stock_map[code].plate_attr0_concept = [
                plate for plate in concept_plates
                if plate in attr0_concept_plates]
            stock_map[code].plate_attr1_concept = [
                plate for plate in concept_plates
                if plate in attr1_concept_plates]

        for path in root.iterdir():
            if path.suffix == '.md':
                parts = path.stem.split('.')
                code = '.'.join(parts[:2])
                if code in stock_map and parts[2] == 'info':
                    with open(path) as fp:
                        stock_map[code].info = markdown.markdown(fp.read())

        for path in filtered_stock_paths(root):
            with open(path) as fp:
                for line in fp.readlines():
                    parts = line.split(',')
                    code = parts[0]
                    if code in stock_map:
                        stock_map[code].market_value \
                            = int(float(parts[2].strip()) / 1e8)

        trace_file = root.parent / 'rank' / 'trace.json'
        with open(trace_file) as fp:
            trace_map = json.load(fp)
            for code in stock_map.keys():
                stock_map[code].traces = [
                    trace | {'strategy': strategy_name_map[trace['strategy']]}
                    for trace in trace_map.get(code, [])]

        stocks = [stock_map[code] for code in stocks]

        with open(output, 'w') as fp:
            json.dump({
                'title': title,
                'type': 'stock',
                'stocks': stocks,
            }, fp)
