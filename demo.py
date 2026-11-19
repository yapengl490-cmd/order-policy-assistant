"""本机免费演示的标准库入口；不会调用模型，也不读取仓库外的私人配置。"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from prepare_verify import prepare

ROOT = Path(__file__).resolve().parent


def compose_command(env_path):
    docker = shutil.which('docker')
    if not docker:
        raise ValueError('未找到 docker。请先安装并启动 Docker Desktop，确认 docker version 可用。')
    return [docker, 'compose', '--env-file', str(env_path), '-f', str(ROOT/'compose.verify.yaml')]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'up', 'check', 'token', 'status', 'stop', 'down'])
    parser.add_argument('--env-file', type=Path, default=ROOT/'local/demo.env')
    args = parser.parse_args(argv)
    env_path = args.env_file.expanduser().resolve()
    if args.action == 'prepare':
        prepare(env_path)
        print('下一步：python3 demo.py up')
        return 0
    if not env_path.is_file():
        parser.error('缺少独立配置，请先执行 python3 demo.py prepare；不会借用已有服务凭据。')
    if args.action == 'token':
        # 显式本机展示；不自动打开网页、写剪贴板、记录或发送凭据。
        values = dict(line.split('=', 1) for line in env_path.read_text().splitlines()
                      if line and not line.startswith('#') and '=' in line)
        token = values.get('VERIFY_ACCESS_TOKEN')
        if not token:
            parser.error('独立配置缺少登录凭据')
        print('仅粘贴到本机演示登录框；不要发到聊天、截图或提交版本库：')
        print(token)
        return 0
    command = compose_command(env_path)
    if args.action == 'up':
        command += ['up', '-d', '--build', '--wait', '--wait-timeout', '120']
    elif args.action == 'check':
        command += ['exec', '-T', 'app', 'python', 'demo_check.py']
    elif args.action == 'status':
        command += ['ps', '-a']
    elif args.action == 'stop':
        command += ['stop']
    else:
        # 不提供 -v：停止并移除本项目容器，保留数据库、凭据和运行数据卷。
        command += ['down', '--remove-orphans']
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode == 0 and args.action == 'up':
        print('已启动。本机默认访问 http://127.0.0.1:18180；若设置 DEMO_PORT 则使用该端口。')
        print('python3 demo.py check 验证；python3 demo.py token 显示本机登录凭据。')
    return result.returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print('演示操作未完成：' + str(error), file=sys.stderr)
        raise SystemExit(1)
