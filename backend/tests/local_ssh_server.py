"""本机真实 SSH 服务（用于备份链路验证）。

与 mock_ssh_servers.py 不同：这个服务器**真实执行收到的命令**，
因此可以验证备份任务产出的确实是真文件，而不是模拟输出。

用途：让平台通过 SSH 在本机执行 `docker exec aiops-mysql mysqldump ...`，
从而对真实 MySQL 完成一次真实的逻辑备份。

⚠️ 仅用于本地测试：它不做认证、任意命令都会执行。切勿用于任何真实环境。

运行：python tests/local_ssh_server.py [port]
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncssh

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 2901


class LocalSSHServer(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return False  # 测试用：不认证


async def handle(process: asyncssh.SSHServerProcess) -> None:
    """真实执行命令并回传输出。"""
    command = process.command
    if not command:
        # 交互式会话：提供一个最简 shell
        process.stdout.write("$ ")
        while True:
            try:
                data = await process.stdin.read(4096)
            except asyncssh.TerminalSizeChanged:
                continue
            except (asyncssh.Error, OSError):
                break
            if not data:
                break
            text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
            if text.strip():
                proc = await asyncio.create_subprocess_shell(
                    text.strip(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await proc.communicate()
                process.stdout.write(out.decode("utf-8", errors="replace") + "\r\n$ ")
        process.exit(0)
        return

    # 非交互式 exec：真实跑
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        stdout, stderr = await proc.communicate()
        if stdout:
            process.stdout.write(stdout.decode("utf-8", errors="replace"))
        if stderr:
            process.stderr.write(stderr.decode("utf-8", errors="replace"))
        process.exit(proc.returncode or 0)
    except Exception as exc:
        process.stderr.write(f"{type(exc).__name__}: {exc}\n")
        process.exit(1)


async def main() -> None:
    key = asyncssh.generate_private_key("ssh-rsa")
    await asyncssh.create_server(
        LocalSSHServer,
        "127.0.0.1",
        PORT,
        server_host_keys=[key],
        process_factory=handle,
        reuse_address=True,
        line_editor=False,
        line_echo=False,
    )
    print(f"本机真实 SSH 服务已监听 127.0.0.1:{PORT}", flush=True)
    print("⚠️ 无认证、任意命令都会执行 —— 仅供本地测试，切勿暴露到网络", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
