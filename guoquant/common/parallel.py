"""
多线程进度显示模块（基于 rich）：并发执行与线程安全进度条。

提供两类并发进度方案：

- ``parallel()``：把同一任务 ``job`` 应用到一组参数（最多 8 线程
  并发），边执行边以进度条显示完成数；
- ``MultiThreadProgress``：线程安全的进度条上下文管理器（rich
  ``Progress`` + ``Live``），可安全地在多线程/多循环中 ``add_task`` /
  ``update``。

统一控制台取自 ``guoquant.common.log`` （``console``），本模块不依赖
行情库。

被引用方：

- ``parallel()`` 供 ``commands/rank.py``、
  ``commands/check_market_sentiment_cycle.py`` 批量读取行情使用；
- ``MultiThreadProgress`` 由 ``commands/fetch_stockdata.py`` 等创建，
  传入 ``fetch/*`` 抓取函数展示进度。
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.live import Live
from rich.progress import *

from guoquant.common.log import console


def parallel(job, args, description='Working...'):
    """并发执行 ``job(arg)``：每个 ``arg`` 一个任务，最多 8 个线程，带进度条。

    结果按任务完成先后返回（而非 ``args`` 顺序）；单个任务抛异常时记录
    日志并跳过，不影响其余任务。

    Args:
        job (callable): 接收单个参数的可调用对象。
        args (list): 参数列表，长度即总任务数。
        description (str): 进度条描述文字，默认 ``'Working...'``。

    Returns:
        list: 各任务返回值的列表。
    """
    results = []
    with Progress(console=console) as progress:
        task = progress.add_task(description, total=len(args))
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(job, arg) for arg in args]
            for f in as_completed(futures):
                try:
                    results.append(f.result())
                except Exception as e:
                    logging.exception("task failed", exc_info=e)
                progress.advance(task)
    return results


class MultiThreadProgress:
    """线程安全的进度条上下文管理器（rich ``Progress`` + ``Live`` 组合）。

    由调用方（如 ``commands/fetch_stockdata.py``）创建，供抓取等循环/多线程
    共享：所有 ``add_task`` / ``update`` 均加锁，避免并发更新导致 rich
    渲染异常。用法示例：

    .. code-block:: python

        with MultiThreadProgress() as p:
            tid = p.add_task('desc', total=N)
            p.update(tid, advance=1)

    Args:
        console (rich.console.Console | None): 输出目标 Console；为 None 时
            使用 rich 默认控制台，默认 None。
        refresh_per_second (int): 每秒刷新次数，默认 10。
        transient (bool): 为 True 时进度条结束后自动清除（不留在终端上），
            默认 False。
    """
    def __init__(self, console=None, refresh_per_second=10, transient=False):
        self._progress = Progress(
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            refresh_per_second=refresh_per_second,
        )
        self._live = Live(self._progress,
                          console=console,
                          refresh_per_second=refresh_per_second,
                          transient=transient)
        self._lock = threading.Lock()

    def add_task(self, description, total):
        """新增一个任务（线程安全）。

        Args:
            description (str): 任务描述文字，显示在进度条上。
            total (int): 任务总进度数。

        Returns:
            int: 新增任务的 task_id，供 ``update`` 使用。
        """
        with self._lock:
            return self._progress.add_task(description, total=total)

    def update(self, task_id, completed=None, advance=None, description=None):
        """更新任务进度（线程安全）。

        Args:
            task_id (int): ``add_task`` 返回的任务 id。
            completed (int | None): 设置的绝对进度值；为 None 时不修改，
                默认 None。
            advance (int | None): 进度增量；为 None 时不修改，默认 None。
            description (str | None): 更新后的任务描述；为 None 时不修改，
                默认 None。
        """
        with self._lock:
            self._progress.update(task_id,
                                  completed=completed,
                                  advance=advance,
                                  description=description)

    def start(self):
        """启动 Live 渲染循环。"""
        self._live.start()

    def stop(self):
        """停止 Live 渲染循环。"""
        self._live.stop()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
