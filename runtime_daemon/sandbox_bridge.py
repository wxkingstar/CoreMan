"""沙箱里的第一个进程：在 127.0.0.1 上开一个转发口，再起并看护一个子进程。

沙箱有自己的网络命名空间，只有回环网卡、没有出口。CLI 按 HTTPS_PROXY 把所有连接发到这个口，
这里原样转给挂进来的 Unix socket，另一端是守护进程的放行代理（egress.py），由它决定放不放行。
子进程（驱动，或自检命令）退出时本进程以同一个退出码退出；收到 SIGTERM / SIGINT 转给子进程。

只用标准库，以 `python -I <本文件>` 运行：沙箱里没有守护进程的依赖，也不读任何环境里的 Python 设置。
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading

BUFFER = 64 * 1024


def _pump(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            data = source.recv(BUFFER)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, target):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _serve_one(client: socket.socket, upstream: str) -> None:
    try:
        remote = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        remote.connect(upstream)
    except OSError:
        client.close()
        return
    threading.Thread(target=_pump, args=(client, remote), daemon=True).start()
    threading.Thread(target=_pump, args=(remote, client), daemon=True).start()


def serve(listener: socket.socket, upstream: str) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        _serve_one(client, upstream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("缺少要看护的命令")
    # 这一轮写下的一切（转录、下载的附件、CLI 的缓存）只有本用户读得到。
    os.umask(0o077)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", args.port))
    listener.listen(64)
    threading.Thread(target=serve, args=(listener, args.upstream), daemon=True).start()
    child = subprocess.Popen(command)

    def forward(signum: int, _frame: object) -> None:
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    code = child.wait()
    listener.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
