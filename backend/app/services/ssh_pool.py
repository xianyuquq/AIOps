"""SSH 执行器与连接池。

关键设计（见 docs/ARCHITECTURE.md §6.1）：
- 使用 asyncssh 的交互式 PTY channel，而非 conn.run()，以获得真实终端语义
  （vim、top、tab 补全、颜色）。
- 连接按 server_id 缓存复用，避免每次开终端都重新握手。
- echo_off 在打开终端后立即执行 `stty -echo`，这是广播模式不出现重复显示的前提。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import stat
import time
from pathlib import Path

import asyncssh

from app.config import settings
from app.core.crypto import decrypt_secret
from app.services.executor_base import (
    ChannelHandle,
    ExecResult,
    Executor,
    FileEntry,
    OutputSink,
    ProgressCallback,
)
from app.services.server_ref import ServerRef, to_ref

logger = logging.getLogger(__name__)


class SSHError(RuntimeError):
    pass


def describe_connect_error(exc: BaseException, server=None) -> str:
    """把底层异常翻译成可操作的中文错误说明。

    为什么需要这个：某些异常（特别是超时）的 str() 是**空字符串**，
    直接 f"连接失败：{exc}" 会产出「连接失败：」这种没有任何信息的提示。
    注意 Python 3.11+ 中 asyncio.TimeoutError 就是内建 TimeoutError，
    而它是 OSError 的子类，因此**必须先于 OSError 判断**。
    """
    target = f"{server.host}:{server.port}" if server is not None else "目标主机"
    user = getattr(server, "username", "") if server is not None else ""

    # ---- 认证类 ----
    if isinstance(exc, asyncssh.PermissionDenied):
        return (
            f"认证失败：用户名或密码/私钥不正确（用户 {user}）。"
            f"请确认与 MobaXterm 中使用的是同一套凭据；"
            f"若服务器只允许密钥登录，请把认证方式改为「私钥」并粘贴私钥内容。"
            f"（服务端返回：{exc or '未提供原因'}）"
        )

    if isinstance(exc, asyncssh.HostKeyNotVerifiable):
        return (
            f"主机密钥校验失败：服务端密钥与已知记录不一致，可能存在中间人风险。"
            f"确认无误后可开启「自动接受主机密钥」。（{exc or '未提供原因'}）"
        )

    if isinstance(exc, asyncssh.KeyExchangeFailed):
        return (
            f"密钥交换失败：客户端与服务端没有共同支持的加密算法。"
            f"老旧设备（如 CentOS 6、部分国产系统）常只支持 ssh-rsa / "
            f"diffie-hellman-group1-sha1，需要在服务端或客户端开启兼容算法。"
            f"（{exc or '未提供原因'}）"
        )

    # ---- 超时（必须早于 OSError：TimeoutError 是 OSError 子类）----
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return (
            f"连接超时：无法在 {settings.ssh_connect_timeout} 秒内完成与 {target} 的 SSH 握手。"
            f"常见原因：① 防火墙只放行了特定来源 IP；"
            f"② 服务端 sshd 的 MaxStartups/LoginGraceTime 限制；"
            f"③ 目标在内网而本机需要走代理或 VPN；"
            f"④ 主机名解析到了错误的地址。"
        )

    if isinstance(exc, asyncssh.ConnectionLost):
        return (
            f"连接被服务端中断：握手过程中连接丢失。"
            f"常见原因：① 服务端触发 fail2ban 之类的封禁；"
            f"② 并发连接数超过 sshd 限制；③ 中间设备重置了连接。"
            f"（{exc or '未提供原因'}）"
        )

    # ---- 网络类 ----
    if isinstance(exc, ConnectionRefusedError):
        return f"连接被拒绝：{target} 端口未开放或 sshd 未运行。"

    if isinstance(exc, ConnectionResetError):
        return (
            f"连接被重置：{target} 主动断开了连接。"
            f"可能是 sshd 限制、防火墙或安全设备拦截。"
        )

    if isinstance(exc, OSError):
        detail = str(exc) or type(exc).__name__
        return f"网络错误（{type(exc).__name__}）：{detail}。目标 {target}。"

    if isinstance(exc, asyncssh.Error):
        detail = str(exc) or "服务端未提供原因"
        return f"SSH 协议错误（{type(exc).__name__}）：{detail}"

    detail = str(exc) or type(exc).__name__
    return f"连接失败（{type(exc).__name__}）：{detail}"


def _build_connect_kwargs(server, password: str | None) -> dict:
    """把 Server 模型转换为 asyncssh.connect 的参数。"""
    kwargs: dict = {
        "host": server.host,
        "port": server.port,
        "username": server.username,
        "known_hosts": None if settings.ssh_auto_accept_host_key else (),
        "connect_timeout": settings.ssh_connect_timeout,
        "keepalive_interval": settings.ssh_keepalive_interval,
    }
    if server.auth_type == "private_key":
        if not password:
            raise SSHError("私钥认证缺少私钥内容")
        try:
            key = asyncssh.import_private_key(password)
        except Exception as exc:
            raise SSHError(f"私钥解析失败：{exc}") from exc
        kwargs["client_keys"] = [key]
    else:
        kwargs["password"] = password
        # 部分老旧设备只支持 keyboard-interactive
        kwargs["preferred_auth"] = "password,keyboard-interactive"
    return kwargs


class SSHExecutor(Executor):
    """单个服务器的 SSH 会话。"""

    def __init__(self, server_id: int, name: str, server, password: str | None) -> None:
        super().__init__(server_id, name)
        self._server = server
        self._password = password
        self._conn: asyncssh.SSHClientConnection | None = None
        self._lock = asyncio.Lock()
        self._channels: set[ChannelHandle] = set()

    async def connect(self) -> None:
        async with self._lock:
            if self._conn is not None and not self._conn.is_closed():
                self.is_connected = True
                return
            kwargs = _build_connect_kwargs(self._server, self._password)
            try:
                self._conn = await asyncssh.connect(**kwargs)
            except Exception as exc:
                raise SSHError(describe_connect_error(exc, self._server)) from exc
            self.is_connected = True

    async def close(self) -> None:
        for handle in list(self._channels):
            with contextlib.suppress(Exception):
                await handle.close()
        self._channels.clear()
        if self._conn is not None:
            self._conn.close()
            with contextlib.suppress(Exception):
                await self._conn.wait_closed()
            self._conn = None
        self.is_connected = False

    def _ensure(self) -> asyncssh.SSHClientConnection:
        if self._conn is None or self._conn.is_closed():
            raise SSHError("SSH 连接已断开")
        return self._conn

    async def run(self, command: str, timeout: int | None = None) -> ExecResult:
        await self.connect()
        conn = self._ensure()
        timeout = timeout or settings.ssh_command_timeout
        started = time.monotonic()
        try:
            # input=b"" 显式关闭 stdin：否则像 ksql / sys_dump 这类
            # **不认 PGPASSWORD、会转成交互式提示**的工具会一直等输入，
            # 表现为命令既不报错也不返回，直到超时——极难排查。
            # 关掉 stdin 后它们会立刻失败并给出明确错误。
            result = await asyncio.wait_for(
                conn.run(command, check=False, input=b""), timeout=timeout
            )
        except asyncio.TimeoutError:
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=f"命令执行超时（{timeout}s）",
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
            )
        except asyncssh.Error as exc:
            raise SSHError(f"执行失败：{exc}") from exc
        return ExecResult(
            exit_code=result.exit_status if result.exit_status is not None else -1,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def open_terminal(
        self,
        sink: OutputSink,
        cols: int = 120,
        rows: int = 30,
        echo_off: bool = False,
    ) -> ChannelHandle:
        await self.connect()
        conn = self._ensure()
        try:
            # create_session 返回 (channel, session)。
            # 读写必须使用 session（SSHClientProcess），它提供 read/write/
            # change_terminal_size；原始 channel 没有 read() 方法。
            _chan, proc = await conn.create_session(
                asyncssh.SSHClientProcess,
                "",
                term_type="xterm-256color",
                term_size=(cols, rows),
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, asyncssh.Error) as exc:
            raise SSHError(f"打开终端失败：{exc}") from exc

        if echo_off:
            # 关闭远端回显：广播模式下由前端本地回显，否则会出现重复显示。
            # PS1 置空是为了让命令边界清晰，避免提示符混入输出。
            proc.stdin.write("stty -echo 2>/dev/null; export PS1=''; clear\n")

        async def _write(data: str) -> None:
            # asyncssh 的 SSHClientProcess 通过 stdin/stdout 流读写，
            # 没有 process.write() / process.read(n) 这两个方法。
            proc.stdin.write(data)

        async def _resize(c: int, r: int) -> None:
            with contextlib.suppress(Exception):
                proc.change_terminal_size(c, r)

        handle = ChannelHandle(
            server_id=self.server_id,
            server_name=self.name,
            write=_write,
            resize=_resize,
            close=self._close_channel_factory(proc),
        )
        self._channels.add(handle)

        asyncio.create_task(self._pump(proc, sink))
        return handle

    def _close_channel_factory(self, proc):
        async def _close() -> None:
            with contextlib.suppress(Exception):
                proc.close()
                await proc.wait_closed()
        return _close

    async def _pump(self, proc, sink: OutputSink) -> None:
        """持续读取远端输出并投递到 sink。"""
        try:
            while True:
                data = await proc.stdout.read(65536)
                if not data:
                    break
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                await sink.push_output(self.server_id, data)
        except asyncio.CancelledError:
            raise
        except (asyncssh.Error, OSError, AttributeError, TypeError) as exc:
            logger.debug("终端 %s 读取结束：%s", self.name, exc)
        finally:
            with contextlib.suppress(Exception):
                await sink.push_status(self.server_id, "closed", "会话已结束")

    async def _sftp(self):
        await self.connect()
        return await self._ensure().start_sftp_client()

    async def upload(self, local_path: str, remote_path: str,
                     on_progress: ProgressCallback | None = None) -> None:
        total = os.path.getsize(local_path)
        sftp = await self._sftp()
        try:
            await sftp.put(local_path, remote_path)
        finally:
            sftp.exit()
        if on_progress:
            await on_progress(total, total)

    async def download(self, remote_path: str, local_path: str,
                       on_progress: ProgressCallback | None = None) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        sftp = await self._sftp()
        try:
            await sftp.get(remote_path, local_path)
        finally:
            sftp.exit()
        if on_progress:
            size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
            await on_progress(size, size)

    async def list_dir(self, remote_path: str) -> list[FileEntry]:
        sftp = await self._sftp()
        entries: list[FileEntry] = []
        try:
            for item in await sftp.readdir(remote_path):
                # 过滤 . 与 ..（多数 SFTP 服务器会返回，但界面上没有意义）
                if item.filename in (".", ".."):
                    continue
                attrs = item.attrs
                is_dir = stat.S_ISDIR(attrs.permissions or 0)
                entries.append(
                    FileEntry(
                        name=item.filename,
                        path=f"{remote_path.rstrip('/')}/{item.filename}",
                        is_dir=is_dir,
                        size=attrs.size or 0,
                        mtime=float(attrs.mtime or 0),
                        mode=oct(attrs.permissions or 0),
                    )
                )
        finally:
            sftp.exit()
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries


class SSHConnectionPool:
    """按 server_id 复用 SSH 连接。

    ⚠️ 注意：连接池是**进程内状态**。若使用多个 uvicorn worker，
    不同 worker 会各自持有连接，导致终端归属混乱。
    本项目的规模定位（10 台以内）要求单 worker 运行。
    """

    def __init__(self, max_total: int = 200) -> None:
        self._executors: dict[int, SSHExecutor] = {}
        self._lock = asyncio.Lock()
        self._max_total = max_total

    async def get(self, server) -> SSHExecutor:
        """获取（或创建）指定服务器的执行器。

        接受 ORM Server 对象或 ServerRef 快照。传入 ORM 对象时必须保证
        Session 仍处于打开状态——快照在此处生成，之后执行器不再依赖 Session。
        """
        ref = to_ref(server)
        async with self._lock:
            existing = self._executors.get(ref.id)
            if existing is not None and existing.is_connected:
                return existing
            if existing is not None:
                with contextlib.suppress(Exception):
                    await existing.close()
                self._executors.pop(ref.id, None)

            if len(self._executors) >= self._max_total:
                raise SSHError(f"SSH 连接数已达上限（{self._max_total}）")

            password = None
            if ref.secret_ciphertext and ref.secret_nonce:
                password = decrypt_secret(ref.secret_ciphertext, ref.secret_nonce)

            executor = SSHExecutor(ref.id, ref.name, ref, password)
            self._executors[ref.id] = executor
            return executor

    async def release(self, server_id: int) -> None:
        async with self._lock:
            executor = self._executors.pop(server_id, None)
        if executor is not None:
            with contextlib.suppress(Exception):
                await executor.close()

    async def close_all(self) -> None:
        async with self._lock:
            executors = list(self._executors.values())
            self._executors.clear()
        for executor in executors:
            with contextlib.suppress(Exception):
                await executor.close()

    @property
    def active_count(self) -> int:
        return len(self._executors)


# 全局单例
pool = SSHConnectionPool(max_total=settings.ssh_max_connections)


# --------------------------------------------------------------------------- 连接诊断


async def diagnose_server(ref, password: str | None) -> dict:
    """分阶段诊断到目标服务器的 SSH 连接。

    分阶段的意义在于把「连不上」拆成可定位的结论：
      TCP 可达 → SSH 握手/算法 → 认证 → 打开会话
    每一阶段独立给出结论，避免只得到一个笼统的失败。
    """
    steps: list[dict] = []
    target = f"{ref.host}:{ref.port}"

    def add(name: str, ok: bool, detail: str) -> None:
        steps.append({"step": name, "ok": ok, "detail": detail})

    # ---------- 阶段 1：TCP 连通 + banner ----------
    banner = ""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ref.host, ref.port),
            timeout=min(settings.ssh_connect_timeout, 10),
        )
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=6)
            banner = raw.decode("utf-8", errors="replace").strip()
        except asyncio.TimeoutError:
            banner = ""
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

        add("1. TCP 连通性", True, f"已连通 {target}")
        if banner:
            add("2. SSH 服务标识", True, banner)
        else:
            add(
                "2. SSH 服务标识",
                False,
                "端口可连接但未返回 SSH 标识，可能该端口不是 SSH 服务",
            )
            return {"ok": False, "target": target, "steps": steps}
    except asyncio.TimeoutError:
        add("1. TCP 连通性", False, f"连接 {target} 超时：防火墙拦截或端口未开放")
        return {"ok": False, "target": target, "steps": steps}
    except ConnectionRefusedError:
        add("1. TCP 连通性", False, f"{target} 拒绝连接：sshd 未运行或端口不对")
        return {"ok": False, "target": target, "steps": steps}
    except OSError as exc:
        add(
            "1. TCP 连通性",
            False,
            f"网络错误（{type(exc).__name__}）：{exc or '无详细信息'}",
        )
        return {"ok": False, "target": target, "steps": steps}

    # ---------- 阶段 3：SSH 握手 + 认证（使用真实凭据）----------
    conn = None
    try:
        kwargs = _build_connect_kwargs(ref, password)
        conn = await asyncssh.connect(**kwargs)
        add("3. SSH 握手与认证", True, f"认证成功（用户 {ref.username}）")
    except Exception as exc:
        add("3. SSH 握手与认证", False, describe_connect_error(exc, ref))
        return {"ok": False, "target": target, "steps": steps}

    # ---------- 阶段 4：打开会话执行一条命令 ----------
    try:
        # 注意：asyncssh 的 run() 返回 SSHCompletedProcess，
        # 它没有 .ok 属性（那是我们自己的 ExecResult 才有的），
        # 必须用 exit_status 判断。
        result = await asyncio.wait_for(conn.run("echo aiops-ok", check=False), timeout=15)
        stdout = result.stdout or ""
        if result.exit_status == 0 and "aiops-ok" in stdout:
            add("4. 执行命令", True, "远程命令执行正常")
            ok = True
        else:
            add(
                "4. 执行命令",
                False,
                f"命令未返回预期结果（退出码 {result.exit_status}）："
                f"{(result.stderr or '').strip()[:200] or stdout.strip()[:200]}",
            )
            ok = False
    except asyncio.TimeoutError:
        add("4. 执行命令", False, "执行命令超时")
        ok = False
    except Exception as exc:
        add("4. 执行命令", False, f"执行失败（{type(exc).__name__}）：{exc}")
        ok = False
    finally:
        if conn is not None:
            conn.close()
            with contextlib.suppress(Exception):
                await conn.wait_closed()

    return {"ok": ok, "target": target, "steps": steps}
