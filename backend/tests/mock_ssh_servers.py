"""独立的 mock SSH 服务器，用于浏览器端到端演示。

启动 4 个模拟 Linux 节点（端口 2501~2504），模拟已关闭回显的 shell。
仅供本地验证使用，切勿用于生产。

运行：python tests/mock_ssh_servers.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import asyncssh

PORTS = [2501, 2502, 2503, 2504]

# SFTP 根目录：每个 mock 节点暴露同一个沙箱目录，便于验证文件上传与浏览。
SFTP_ROOT = os.environ.get(
    "MOCK_SFTP_ROOT", os.path.join(tempfile.gettempdir(), "aiops_mock_sftp")
)


def prepare_sftp_root() -> None:
    os.makedirs(SFTP_ROOT, exist_ok=True)
    samples = {
        "README.txt": "这是 mock 服务器的示例文件，用于验证 SFTP 浏览与上传。\n",
        "app.log": "\n".join(
            f"2026-10-06 12:{i:02d}:00 INFO  request handled id={i}" for i in range(30)
        )
        + "\n",
        "config.yaml": "server:\n  port: 8080\n  workers: 4\n",
    }
    for name, content in samples.items():
        path = os.path.join(SFTP_ROOT, name)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
    os.makedirs(os.path.join(SFTP_ROOT, "logs"), exist_ok=True)
    os.makedirs(os.path.join(SFTP_ROOT, "data"), exist_ok=True)


def make_sftp_factory():
    def factory(chan: asyncssh.SSHServerChannel) -> asyncssh.SFTPServer:
        return asyncssh.SFTPServer(chan, chroot=SFTP_ROOT)

    return factory

FAKE_DF = """Filesystem     Type      Size  Used Avail Use% Mounted on
/dev/sda1      ext4       50G   46G  1.2G  95% /
/dev/sdb1      xfs       500G  120G  355G  26% /data"""

FAKE_FREE = """              total        used        free      shared  buff/cache
Mem:          16000        9000        7000         200        3000"""

FAKE_COMMANDS = {
    "df": FAKE_DF,
    "free": FAKE_FREE,
    "uname": "Linux node 5.15.0-91-generic #101-Ubuntu SMP x86_64 GNU/Linux",
    "whoami": "root",
    "hostname": None,
    "uptime": " 14:32:01 up 12 days,  3:21,  2 users,  load average: 0.42, 0.38, 0.35",
    "pwd": "/root",
    "id": "uid=0(root) gid=0(root) groups=0(root)",
}


class MockSSHServer(asyncssh.SSHServer):
    def __init__(self, node: str) -> None:
        self.node = node

    def begin_auth(self, username: str) -> bool:
        return False


def make_handler(node: str):
    async def handle(process: asyncssh.SSHServerProcess) -> None:
        process.stdout.write(f"[{node}]$ ")
        buffer = ""
        while True:
            try:
                data = await process.stdin.read(4096)
            except asyncssh.TerminalSizeChanged:
                # asyncssh 以「在读取中抛出异常」的方式通知窗口尺寸变化。
                # 必须捕获并继续读取，否则处理协程会终止、连接被关闭。
                continue
            except (asyncssh.BreakReceived, asyncssh.SignalReceived):
                continue
            except (asyncssh.Error, OSError):
                break
            if not data:
                break
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")

            buffer += data
            while "\r" in buffer or "\n" in buffer:
                positions = [i for i in (buffer.find("\r"), buffer.find("\n")) if i >= 0]
                idx = min(positions)
                line, buffer = buffer[:idx], buffer[idx + 1:]
                line = line.strip()
                if not line:
                    continue

                if line.startswith("stty") or "export PS1" in line or line == "clear":
                    continue

                head = line.split()[0]
                if head == "hostname":
                    process.stdout.write(node + "\r\n")
                elif head in FAKE_COMMANDS:
                    process.stdout.write(FAKE_COMMANDS[head] + "\r\n")
                else:
                    process.stdout.write(f"bash: {line}: command not found\r\n")
                process.stdout.write(f"[{node}]$ ")

    return handle


async def main() -> None:
    prepare_sftp_root()
    key = asyncssh.generate_private_key("ssh-rsa")
    servers = []
    for i, port in enumerate(PORTS, start=1):
        node = f"web-mock-0{i}"
        server = await asyncssh.create_server(
            lambda: MockSSHServer(node),
            "127.0.0.1",
            port,
            server_host_keys=[key],
            process_factory=make_handler(node),
            sftp_factory=make_sftp_factory(),
            reuse_address=True,
            line_editor=False,
            line_echo=False,
        )
        servers.append(server)
        print(f"mock SSH 节点 {node} 监听 127.0.0.1:{port}（SFTP 根：{SFTP_ROOT}）", flush=True)

    print("mock SSH 服务器已就绪（Ctrl+C 停止）", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
