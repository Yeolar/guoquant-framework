"""远端数据同步模块：sshpass + rsync 从远端主机（Mac mini）拉取数据目录。

所需环境变量（``.env`` 由 CLI 统一加载）：

- ``MACMINI_IP``：远端主机地址；
- ``MACMINI_USERNAME``：SSH 用户名；
- ``MACMINI_PASSWORD``：SSH 密码。

三者缺任一即跳过同步并打印提示（``pipe_select_stocks`` 仅在本地目录缺失时
调用本模块，故本地已有数据时未配置也无害）。
"""
import os
import subprocess

from guoquant.common.log import console


def fetch_remote(local_path, remote_path):
    """经 sshpass（``-p`` 传密码）执行 rsync，把远端绝对路径拉到本地目录。

    依赖本机安装 ``sshpass``；同步成功打印 ``synced ...``，失败仅打印错误、
    不抛异常；配置缺失时打印黄色提示并直接返回。

    Args:
        local_path (str): 本地目标目录路径（rsync 目标端）。
        remote_path (str): 远端绝对路径（rsync 源端 ``user@host:path`` 中
            的 ``path`` 部分）。
    """
    hostname = os.environ.get('MACMINI_IP', '')
    username = os.environ.get('MACMINI_USERNAME', '')
    password = os.environ.get('MACMINI_PASSWORD', '')
    if not hostname or not username or not password:
        console.print('MACMINI_* 环境变量未配置，跳过远端同步', style='yellow bold')
        return
    cmd = [
        'sshpass', '-p', password,
        'rsync', '-a', '--partial', '--info=progress2',
        f'{username}@{hostname}:{remote_path}', local_path,
    ]
    try:
        subprocess.run(cmd, check=True)
        console.print(f'synced {remote_path} -> {local_path}', style='green')
    except subprocess.CalledProcessError as e:
        console.print(f'fetch error: {e}', style='red bold')
