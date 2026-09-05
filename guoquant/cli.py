"""
guoquant quant 框架命令行入口（typer 版）。

用法：
        python -m guoquant.cli <command> [options]

命令发现：扫描 guoquant.commands 包下每个模块（不含 _ 开头），
模块需导出 `command` 函数（typer 风格，函数签名即参数、docstring 即 help）。

与框架其他部分的关系：
  - 依赖：guoquant.commands.*（各业务命令）、typer（CLI 框架）；
  - 被引用：__main__ 或外部脚本通过 `python -m guoquant.cli` 调用，
    新增命令只需在 commands/ 下新建模块并导出 command()，无需改本文件。
"""
import importlib
import pkgutil

import typer
from dotenv import load_dotenv

# 加载工作区 .env（FETCH_PROVIDER 等配置；不存在时静默跳过）
load_dotenv('.env')

app = typer.Typer(
    name="guoquant",
    help="guoquant quant 框架命令",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def _main(
    verbosity: int = typer.Option(
        1,
        "-v",
        "--verbosity",
        min=0,
        max=3,
        help=(
            "Verbosity level; 0=minimal output, 1=normal output, "
            "2=verbose output, 3=very verbose output"
        ),
    ),
    traceback: bool = typer.Option(
        False, "--traceback", help="Raise on command errors."
    ),
) -> None:
    """guoquant quant 框架命令（typer 版）。

    全局回调：为所有子命令提供统一的 -v/--verbosity 与 --traceback 选项。
    注意：当前版本仅声明参数，各命令自行决定如何使用 verbosity 控制输出量。
    """


def _register() -> None:
    """自动发现并注册 commands 包下的全部子命令。

    约定：commands 下每个非 _ 开头模块须导出 `command` 函数，
    模块文件名即子命令名，command 的 docstring 首行作为 help 文本。
    该函数在模块导入时执行一次（见下方 _register() 调用）。
    """
    import guoquant.commands as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name.startswith("_"):
            # 下划线开头的模块视为内部模块，不注册为命令
            continue
        m = importlib.import_module(f"guoquant.commands.{mod.name}")
        fn = getattr(m, "command", None)
        if fn is None:
            raise ImportError(f"命令模块 {mod.name} 缺少 command 函数")
        app.command(mod.name, help=(fn.__doc__ or "").strip())(fn)


_register()


def main() -> None:
    """CLI 入口：直接执行 typer 应用（解析 sys.argv 并分发到子命令）。"""
    app()


if __name__ == "__main__":
    main()
