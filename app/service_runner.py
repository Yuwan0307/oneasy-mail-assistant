"""Foreground launchd entry; reserve the local port before starting monitoring."""
import argparse
import os
import socket
from pathlib import Path

import uvicorn
from .config import initialize_config
from .store import Store
from .web import create_app


def run(data, port):
    with socket.socket() as listener:
        option = socket.SO_EXCLUSIVEADDRUSE if os.name == 'nt' else socket.SO_REUSEADDR
        listener.setsockopt(socket.SOL_SOCKET, option, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen(128)
        os.umask(0o077)
        data = Path(data)
        data.mkdir(parents=True, exist_ok=True)
        config = initialize_config(data / 'config.json')
        app = create_app(Store(data / 'main.db'), config, run_worker=True, desktop_control=True, desktop_port=port)
        uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                     access_log=False, log_level='warning')).run(sockets=[listener])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Oneasy 邮件助手后台')
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('本机端口无效')
    run(args.data, args.port)
