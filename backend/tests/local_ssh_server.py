"""本机真实 SSH 服务 —— 用 Git Bash 执行命令（用于备份链路验证）。

与 mock_ssh_servers.py 不同：这个服务器**真实执行收到的命令**，
因此可以验证备份任务产出的确实是真文件，而不是模拟输出。

为什么必须走 Git Bash：
    平台的备份脚本是 POSIX shell 语法（set -e / 管道 / find -mtime）。
    Windows 默认的 cmd.exe 完全无法执行，之前因此无法在本机验证 MySQL 备份。
    装上 Git for Windows 后 E:\\gitBash\\bin\\bash.exe 就是完整的 POSIX shell，
    于是「本机」也能作为一个合格的备份执行主机。

⚠️ 仅用于本地测试：不做认证、任意命令都会执行。切勿暴露到网络。

运行：python tests/local_ssh_server.py [port]
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncssh

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 2901

# 优先用 E:\gitBash 下的 bash（本项目约定的安装位置），找不到再退回 PATH
_BASH_CANDIDATES = [
    r"E:\gitBash\bin\bash.exe",
    r"C:\Program Files\Git\bin\bash.exe",
]


def find_bash() -> str:
    for p in _BASH_CANDIDATES:
        if Path(p).exists():
            return p
    from shutil import which

    found = which("bash")
    if found:
        return found
    raise SystemExit("未找到 bash（Git Bash）。请先安装 Git for Windows。")


BASH = find_bash()


class LocalSSHServer(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return False  # 测试用：不认证


def _bash_env() -> dict:
    env = os.environ.copy()
    # 让 Git Bash 里的 docker 等 Windows 程序能正常解析路径
    env.setdefault("MSYSTEM", "MINGW64")
    # 测试用的 bin 目录：里面放着 mysqldump 包装脚本（转发到容器）。
    # 相当于模拟「数据库主机上已经装了客户端」这一前提。
    extra = "/e/Agent/.tmp/bin"
    env["PATH"] = extra + ":" + env.get("PATH", "")
    return env


async def _run_bash(command: str) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        BASH,
        "-c",
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_bash_env(),
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out, err


async def handle(process: asyncssh.SSHServerProcess) -> None:
    """真实执行命令并回传输出。"""
    command = process.command

    if not command:
        # 交互式会话：提供一个最简 bash 提示符
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
                code, out, err = await _run_bash(text.strip())
                process.stdout.write(out.decode("utf-8", errors="replace"))
                if err:
                    process.stdout.write(err.decode("utf-8", errors="replace"))
                process.stdout.write(f"\r\n$ ")
        process.exit(0)
        return

    try:
        code, out, err = await _run_bash(command)
        if out:
            process.stdout.write(out.decode("utf-8", errors="replace"))
        if err:
            process.stderr.write(err.decode("utf-8", errors="replace"))
        process.exit(code)
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
    print(f"命令通过 Git Bash 执行：{BASH}", flush=True)
    print("⚠️ 无认证、任意命令都会执行 —— 仅供本地测试", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
