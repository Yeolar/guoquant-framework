"""行情数据复制命令

把某交易日的整个 ``fetched_data`` 目录（quote / flow / dist / plate 等全部
子目录）复制到默认数据根 ``out/fetched_data``，用于把历史某个日期的数据
"平移到当前默认数据目录"，供按固定路径读取。

注意：目标目录会先整目录删除再重建（destructive），保证复制结果与源完全
一致（无残留旧文件）。
"""
from datetime import date
import shutil
import typer
from guoquant.common.log import console
from guoquant.common.utils import outp, todate


def command(
    date: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--date',
        help=f'date, default={date.today():%y%m%d}'),
) -> None:
    """把指定日期的整个 fetched_data 目录复制到默认数据根 ``out/fetched_data``。

    源为 ``out/<日期>/fetched_data``（含 quote/{d,w}/{stock,index}、flow、
    dist、plate 等全部子目录），目标为 ``out/fetched_data``。目标先整体
    删除再以 ``copytree`` 重建，保证复制结果与源完全一致（无残留旧文件）；
    源目录不存在时仅提示并跳过。

    Args:
        date (str): 源数据所在日期（YYMMDD），default=今天。
    """
    d = todate(date)

    source_dir = outp(f'{d:%y%m%d}/fetched_data')
    target_dir = outp('fetched_data')

    if not source_dir.exists():
        console.print(f'source directory not exist: {source_dir}',
                      style='red bold')
        return

    # 目标整体重建，保证复制结果与源完全一致（无残留旧文件）
    if target_dir.exists():
        shutil.rmtree(target_dir)
    shutil.copytree(source_dir, target_dir)

    console.print(f'copied: {source_dir} -> {target_dir}')
