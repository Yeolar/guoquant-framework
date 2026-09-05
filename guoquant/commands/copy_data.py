"""行情数据复制命令

把某交易日的 ``fetched_data/quote/{d,w}/stock/`` 行情文件复制到默认数据根
``out/fetched_data/quote/{d,w}/stock/`` （含 ``index/`` 子目录一并复制），用于
把历史某个日期的行情"平移到当前默认数据目录"，供按固定路径读取。

注意：目标目录会先整目录删除再重建（destructive）；仅复制 d/w 两种周期的行情
（``stock/`` 与 ``index/``），不含 flow/dist。
"""
from datetime import date
import shutil
from pathlib import Path
import typer
from guoquant.common.log import console
from guoquant.common.utils import outp, todate


def command(
    date: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--date',
        help=f'date, default={date.today():%y%m%d}'),
) -> None:
    """把指定日期的 d/w 周期行情复制到默认数据根。

    源为 ``out/<日期>/fetched_data/quote/{d,w}/stock/`` （含同名 ``index/``
    指数目录），目标为 ``out/fetched_data/quote/{d,w}/``。每个目标 ``stock/``
    目录先整体删除再重建，保证复制结果与源完全一致（无残留旧文件）；``index/``
    目录同样整体重建。源目录不存在时仅提示并跳过该周期。

    Args:
        date (str): 源数据所在日期（YYMMDD），default=今天。
    """
    d = todate(date)

    source_base = outp(f'{d:%y%m%d}/fetched_data/quote')
    target_base = outp('fetched_data/quote')

    for sub_dir in ['d', 'w']:
        source_dir = source_base / sub_dir / 'stock'
        target_dir = target_base / sub_dir / 'stock'

        # 目标目录整体重建，保证复制结果与源完全一致（无残留旧文件）
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        if not source_dir.exists():
            console.print(f'source directory not exist: {source_dir}',
                          style='red bold')
            continue

        # 源目录结构为 <周期>/stock/<股票>.parquet（新目录约定），
        # 股票文件名（含代码与市场前缀）保持唯一，直接平铺复制
        for file in source_dir.iterdir():
            if file.is_file():
                shutil.copy2(file, target_dir / file.name)

        # 同步复制指数行情（quote/{d,w}/index/）
        source_index = source_base / sub_dir / 'index'
        target_index = target_base / sub_dir / 'index'
        if source_index.exists():
            if target_index.exists():
                shutil.rmtree(target_index)
            shutil.copytree(source_index, target_index)
            console.print(f'index copied: {source_index} -> {target_index}')

        console.print(f'copied: {source_dir} -> {target_dir}')
