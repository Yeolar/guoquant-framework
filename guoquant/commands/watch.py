"""持仓看盘管理命令

连接 QMT 持续监控持仓，当触发止损时自动卖出。
依赖：``guoquant.common.trade.executor`` （QMT 连接与下单）、
``guoquant.common.trade.strategy`` （``StopLossManager`` 止损状态、
``compute_watch_plan``）。

用法示例：

.. code-block:: text

    # 每分钟轮询一次
    python -m guoquant.cli watch --mini-path /path/to/QMT/userdata_mini \\
        --account 12345678

    # 每 30 秒轮询，从指定文件加载止损状态
    python -m guoquant.cli watch --mini-path /path/to/QMT/userdata_mini \\
        --account 12345678 --positions-file \\
        out/260719/auto_trade/stop_loss_state.json --interval 30

    # 仅检查一次（适合定时任务）
    python -m guoquant.cli watch --mini-path /path/to/QMT/userdata_mini \\
        --account 12345678 --once
"""
import json
import re
import textwrap
import time
from datetime import date, datetime
from pathlib import Path

import typer

from guoquant.common.log import console
from guoquant.common.utils import outp, todate

from guoquant.common.fetch.qmtapi.context import to_qmt_code


_QMT_CODE_RE = re.compile(r'^(\d{6})\.(SZ|SH)$')


def _qmt_to_futu(qmt_code):
    """把 QMT 的 ``000001.SZ`` 代码转成内部统一格式 ``SZ.000001``。

    Args:
        qmt_code (str): QMT 格式代码（6 位数字 + 市场后缀）。

    Returns:
        str: Futu 格式代码；非 A 股格式（无法匹配）时原样返回。
    """
    m = _QMT_CODE_RE.match(qmt_code)
    if m:
        return f'{m.group(2)}.{m.group(1)}'
    return qmt_code


def command(
    mini_path: str = typer.Option(
        ...,
        '--mini-path',
        help='QMT 迷你端 userdata_mini 路径',
    ),
    account: str = typer.Option(
        ...,
        '--account',
        help='资金账号',
    ),
    positions_file: str = typer.Option(
        '{d:%y%m%d}/auto_trade/stop_loss_state.json',
        '--positions-file',
        help='持仓止损状态文件路径，'
             'default={d:%%y%%m%%d}/auto_trade/stop_loss_state.json',
    ),
    interval: int = typer.Option(
        60,
        '--interval',
        help='轮询间隔（秒），default=60',
    ),
    once: bool = typer.Option(
        False,
        '--once',
        help='仅检查一次并退出（适合定时任务）',
    ),
    date: str = typer.Option(
        f'{date.today():%y%m%d}',
        '--date',
        help=f'用于路径模板的日期，default={date.today():%y%m%d}',
    ),
) -> None:
    """持仓看盘止损监控：连接 QMT 持续检查止损条件，触发时自动卖出。

    QMT 需先启动并登录。每个轮询周期获取持仓与最新价：对无止损记录的新持仓
    自动按当前均价注册追踪，止损价随持仓期最高价上移（移动止损）；命中止损的
    持仓以 ``stop_loss`` 原因下单卖出，并从追踪记录移除。持仓止损状态保存到
    ``--positions-file`` 指定的 JSON 文件。``--once`` 模式仅检查一次并退出
    （适合定时任务）；Ctrl+C 或异常退出时也会断开 QMT 并落盘止损状态。

    Args:
        mini_path (str): QMT 迷你端 ``userdata_mini`` 路径。
        account (str): 资金账号。
        positions_file (str): 持仓止损状态文件路径，
            default=``{d:%y%m%d}/auto_trade/stop_loss_state.json``。
        interval (int): 轮询间隔（秒），default=60。
        once (bool): 仅检查一次并退出（适合定时任务）。
        date (str): 用于路径模板的日期（YYMMDD），default=今天。
    """
    d = todate(date)
    mini_path = mini_path
    account_id = account
    interval = interval
    once = once

    positions_file = Path(
        positions_file.format(d=d))

    console.print(f'[看盘止损] 账户={account_id}  间隔={interval}s  '
                   f'mode={"单次" if once else "持续"}')
    console.print(f'           状态文件: {positions_file}')
    console.print('')

    # ── 1. 连接 QMT ─────────────────────────────────────────────────
    from guoquant.common.trade.executor import QmtExecutor
    executor = QmtExecutor(mini_path, account_id)
    if not executor.connect():
        console.print('Error: 连接 QMT 交易接口失败，请检查 QMT 是否已启动', style='red bold')
        raise typer.Exit(1)

    asset = executor.query_asset()
    if asset:
        console.print(f'  账户连接成功  总资产: {asset["total_asset"]:>12,.2f}  '
                       f'可用资金: {asset["cash"]:>12,.2f}')

    # ── 2. 加载止损状态 ──────────────────────────────────────────────
    from guoquant.common.trade.strategy import StopLossManager, compute_watch_plan
    # 加载 auto_trade 保存的止损状态（无文件时 StopLossManager 为空，稍后自动初始化）
    sl_manager = StopLossManager.load(positions_file)
    if sl_manager._entries:
        console.print(f'  已加载 {len(sl_manager._entries)} 条止损追踪记录')
    else:
        console.print('  止损状态文件为空或不存在，将从当前持仓初始化', style='yellow bold')

    poll_count = 0
    try:
        while True:
            poll_count += 1
            now = datetime.now().strftime('%H:%M:%S')
            today = datetime.today().date()

            # ── 3. 获取持仓和价格 ────────────────────────────────────
            positions_qmt = executor.query_positions()
            raw_positions = {}
            for qmt_code, pos in positions_qmt.items():
                futu_code = _qmt_to_futu(qmt_code)
                if pos['volume'] > 0:
                    raw_positions[futu_code] = {
                        'volume': pos['volume'],
                        'avg_price': pos['open_price'],
                    }

            if not raw_positions:
                # 空仓：首次提示一次，--once 模式直接退出，否则等待下一轮
                if poll_count == 1:
                    console.print(f'  [{now}] 当前无持仓')
                if once:
                    break
                time.sleep(interval)
                continue

            # 对无止损记录的新持仓自动注册（以当前均价作为止损基准成本）
            for code, pos in raw_positions.items():
                if code not in sl_manager._entries and pos['volume'] > 0:
                    sl_manager.register_buy(code, pos['avg_price'],
                                            pos['volume'], today)

            all_codes = list(raw_positions.keys())
            from guoquant.common.trade.executor import fetch_current_prices
            latest_prices = fetch_current_prices(all_codes)

            # 更新最高价：止损价随持仓期最高价上移（移动止损/回撤止损依据）
            for code, price in latest_prices.items():
                if code in sl_manager._entries:
                    sl_manager.update_high(code, price)

            # ── 4. 检查止损 ──────────────────────────────────────────
            watch_plan = compute_watch_plan(
                raw_positions, sl_manager, today, latest_prices)

            # ── 5. 输出状态 ──────────────────────────────────────────
            total_mv = sum(
                raw_positions[c]['volume'] * latest_prices.get(c, 0)
                for c in raw_positions
            )
            console.print(
                f'  [{now}] 第 {poll_count} 次检查  '
                f'持仓 {len(raw_positions)} 只  '
                f'市值 ≈{total_mv:,.0f}')

            if watch_plan['stop_loss_codes']:
                console.print(
                    f'  ⚠ 止损触发: '
                    f'{", ".join(watch_plan["stop_loss_codes"])}',
                    style='yellow bold',
                )

            # 输出每只持仓明细
            for code in sorted(watch_plan['hold']):
                d = watch_plan['hold_details'].get(code, {})
                sl = d.get('stop_price')
                sl_str = f'止损={sl:.2f}' if sl else ''
                console.print(
                    f'    {code}  '
                    f'{raw_positions[code]["volume"]}股  '
                    f'现价={d.get("price", 0):.2f}  '
                    f'市值≈{d.get("mv", 0):,.0f}  '
                    f'{sl_str}')

            # ── 6. 执行止损卖出 ───────────────────────────────────────
            if watch_plan['sell']:
                console.print(f'  执行止损卖出 ({len(watch_plan["sell"])} 笔)...')

                # watch_plan['sell'] 为 (code, volume) 列表，统一带 stop_loss 原因下单
                sell_orders = [
                    (code, vol, 'stop_loss')
                    for code, vol in watch_plan['sell']
                ]
                result = executor.execute_orders(
                    sell_orders, [],
                    verbose_cb=lambda stage, info:
                        console.print(f'    [{stage}] {info}'),
                )
                console.print(f'  止损卖出 {result["sold"]} 笔', style='green')
                if result['errors']:
                    for err in result['errors']:
                        console.print(f'    {err}', style='red bold')

                # 从止损管理器移除已卖出的
                for code, vol in watch_plan['sell']:
                    sl_manager.register_sell(code)

            # ── 7. 保存状态 ──────────────────────────────────────────
            # 清除没有持仓的止损记录（已清仓的股票不再追踪）
            for code in list(sl_manager._entries.keys()):
                if code not in raw_positions:
                    sl_manager.register_sell(code)
            sl_manager.save(positions_file)

            if once:
                console.print(f'  状态已保存: {positions_file}', style='green')
                break

            time.sleep(interval)

    except KeyboardInterrupt:
        # Ctrl+C 正常退出：不视为错误，静默停止监控
        console.print('')
        console.print('  监控已停止', style='yellow bold')
    finally:
        # 无论正常/异常退出都断开 QMT 并落盘止损状态，避免状态丢失
        executor.disconnect()
        sl_manager.save(positions_file)
        console.print(f'  状态已保存: {positions_file}')
