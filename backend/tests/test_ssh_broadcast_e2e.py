"""真实 SSH 广播端到端测试。

这是需求 2 最强的验证：不使用任何 mock，完整走通
    终端输入 → TerminalHub 广播 → 真实 SSH 通道 → 远端输出 → 回传
整条链路。

做法：用 asyncssh 在本机起 4 个「假 Linux 服务器」，每个监听不同端口，
模拟 shell 行为（关闭回显、执行命令并返回输出）。然后让平台的真实
SSHConnectionPool 连接它们。

注意：所有测试必须在**同一个事件循环**内完成（Windows 的 ProactorEventLoop
不允许跨循环使用 socket），因此服务器在每个测试内部启动与关闭。
"""

from __future__ import annotations

import asyncio
import sys
import shutil
import tempfile
import time
from pathlib import Path

import asyncssh
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.terminal import TerminalHub

# ─────────────────────────────────────────────── 模拟的远端行为

FAKE_DF = """Filesystem     Type      Size  Used Avail Use% Mounted on
/dev/sda1      ext4       50G   46G  1.2G  95% /
/dev/sdb1      xfs       500G  120G  355G  26% /data"""

FAKE_UNAME = "Linux web-node 5.15.0-91-generic #101-Ubuntu SMP x86_64 GNU/Linux"

FAKE_COMMANDS = {
    "df": FAKE_DF,
    "uname": FAKE_UNAME,
    "hostname": None,  # 动态填为各节点名
    "free": "              total        used        free\nMem:          16000        9000        7000",
    "whoami": "root",
}


class MockSSHServer(asyncssh.SSHServer):
    """不做认证的假服务器（仅测试用）。"""

    def __init__(self, node_name: str) -> None:
        self.node_name = node_name

    def begin_auth(self, username: str) -> bool:
        return False  # 无需认证


# 记录每个节点收到的命令行，用于断言平台确实下发了 stty -echo
RECEIVED: dict[str, list[str]] = {}


def make_session_handler(node_name: str):
    """模拟一个「回显已关闭」的 shell。

    真实 Linux 上回显由 stty -echo 关闭；asyncssh 的对应能力是建服务器时的
    line_editor / line_echo 选项（无法在会话中途切换）。因此 mock 直接以
    line_echo=False 启动，等价于平台建会话后所处的状态——这正是我们要验证的
    运行条件。平台是否**真的下发了** stty -echo，则通过 RECEIVED 记录来断言。
    """

    async def handle_client(process: asyncssh.SSHServerProcess) -> None:
        process.stdout.write(f"[{node_name}]$ ")
        buffer = ""
        while True:
            try:
                data = await process.stdin.read(4096)
            except asyncssh.TerminalSizeChanged:
                # asyncssh 以「读取中抛异常」的方式通知窗口尺寸变化；
                # 不捕获会导致处理协程终止、连接被关闭。
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

                RECEIVED.setdefault(node_name, []).append(line)

                # 平台建会话时发送的引导命令：不产生输出
                if line.startswith("stty") or "export PS1" in line or line == "clear":
                    continue

                head = line.split()[0]
                if head in FAKE_COMMANDS:
                    out = FAKE_COMMANDS[head]
                    if head == "hostname":
                        out = node_name
                    process.stdout.write(out + "\r\n")
                else:
                    process.stdout.write(f"bash: {line}: command not found\r\n")

                process.stdout.write(f"[{node_name}]$ ")

    return handle_client


class CollectingSink:
    def __init__(self) -> None:
        self.outputs: dict[int, str] = {}
        self.statuses: list[tuple[int, str, str]] = []

    async def push_output(self, server_id: int, data: str) -> None:
        self.outputs[server_id] = self.outputs.get(server_id, "") + data

    async def push_status(self, server_id: int, status: str, message: str = "") -> None:
        self.statuses.append((server_id, status, message))


class FakeServer:
    """模拟 Server ORM 对象，直接指向本机 mock SSH 端口。"""

    def __init__(self, sid: int, name: str, port: int) -> None:
        self.id = sid
        self.name = name
        self.host = "127.0.0.1"
        self.port = port
        self.username = "tester"
        self.auth_type = "password"
        self.protocol = "ssh"
        self.platform = "linux"
        self.secret_ciphertext = None
        self.secret_nonce = None


# ─────────────────────────────────────────────── 单循环测试脚手架

_port_cursor = 2400


def _scratch_dir(name: str) -> Path:
    """仓库内的固定临时目录。

    不用 tempfile.mkdtemp：它每次运行都新建一个带随机后缀的目录且从不回收，
    此前在 backend/ 下积了近百个 aiops_src_* / aiops_dst_* / aiops_mock_sftp_*。
    固定目录用完即覆盖，不会累积，也在 .gitignore 覆盖范围内。
    """
    p = Path(__file__).resolve().parents[2] / ".pytest-tmp" / name
    shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _next_ports(count: int = 4) -> list[int]:
    """为每个测试分配独立端口段，避免 TIME_WAIT 冲突。"""
    global _port_cursor
    ports = list(range(_port_cursor, _port_cursor + count))
    _port_cursor += 10
    return ports


async def run_with_servers(scenario_fn, node_count: int = 4, with_sftp: bool = False):
    """在同一个事件循环内：起 mock 服务器 → 跑场景 → 关服务器。"""
    key = asyncssh.generate_private_key("ssh-rsa")
    ports = _next_ports(node_count)
    servers = []
    port_map = {}
    RECEIVED.clear()

    sftp_root = None
    if with_sftp:
        # 用仓库内的固定目录，而不是 mkdtemp：
        # 后者每次运行都新建一个 aiops_mock_sftp_* 且从不回收，
        # 此前在 backend/ 下积了 67 个。固定目录用完即覆盖，不会累积。
        sftp_root = Path(__file__).resolve().parents[2] / ".pytest-tmp" / "sftp"
        shutil.rmtree(sftp_root, ignore_errors=True)
        sftp_root.mkdir(parents=True, exist_ok=True)
        Path(sftp_root, "sample.txt").write_text("hello sftp", encoding="utf-8")
        Path(sftp_root, "logs").mkdir(exist_ok=True)

    for i, port in enumerate(ports, start=1):
        node = f"web-0{i}"
        kwargs = {}
        if sftp_root:
            kwargs["sftp_factory"] = lambda chan, root=sftp_root: asyncssh.SFTPServer(
                chan, chroot=root
            )
        server = await asyncssh.create_server(
            lambda: MockSSHServer(node),
            "127.0.0.1",
            port,
            server_host_keys=[key],
            process_factory=make_session_handler(node),
            reuse_address=True,
            # mock 以「回显已关闭」启动，等价于平台下发 stty -echo 后的状态
            line_editor=False,
            line_echo=False,
            **kwargs,
        )
        servers.append(server)
        port_map[i] = port

    try:
        return await scenario_fn(port_map)
    finally:
        for s in servers:
            try:
                s.close()
            except Exception:
                pass
        await asyncio.gather(
            *[asyncio.wait_for(s.wait_closed(), timeout=5) for s in servers],
            return_exceptions=True,
        )


async def open_and_broadcast(port_map, command="df -Th", wait=2.0):
    """建立 4 台会话并广播命令，返回 (就绪数, sink, session)。"""
    from app.services.ssh_pool import pool

    hub = TerminalHub()
    sink = CollectingSink()
    servers = [FakeServer(i, f"web-0{i}", port_map[i]) for i in sorted(port_map)]

    session = await hub.create_session(servers, sink, mode="mirror", cols=100, rows=30)
    ready = len(session.ready_slots)

    if ready == 0:
        errors = [s.error for s in session.slots.values()]
        await hub.close_all()
        await pool.close_all()
        pytest.fail(f"没有任何 mock SSH 服务器连接成功：{errors}")

    await hub.broadcast_input(session.session_id, command, operator=1)
    await hub.broadcast_input(session.session_id, "\r", operator=1)
    await asyncio.sleep(wait)

    await hub.close_all()
    await pool.close_all()
    return ready, sink, session


# ─────────────────────────────────────────────── 测试


def test_broadcast_reaches_all_mock_ssh_servers():
    """★ 核心验证：一次广播，4 台真实 SSH 会话都执行了命令并返回输出。"""

    async def scenario(port_map):
        ready, sink, _ = await open_and_broadcast(port_map)
        assert ready == 4, f"应连上 4 台，实际 {ready}"
        for sid in (1, 2, 3, 4):
            out = sink.outputs.get(sid, "")
            assert "Filesystem" in out, f"server {sid} 未返回 df 输出：{out!r}"
            assert "/dev/sda1" in out, f"server {sid} 输出内容不完整"
        print("\n各终端输出长度：", {k: len(v) for k, v in sorted(sink.outputs.items())})

    asyncio.run(run_with_servers(scenario))


def test_each_server_returns_its_own_identity():
    """输出按 server_id 正确分流，各终端拿到自己那台的结果。"""

    async def scenario(port_map):
        _, sink, _ = await open_and_broadcast(port_map, command="hostname")
        for sid in (1, 2, 3, 4):
            out = sink.outputs.get(sid, "")
            assert f"web-0{sid}" in out, f"server {sid} 输出未包含自身主机名：{out!r}"

    asyncio.run(run_with_servers(scenario))


def test_platform_disables_remote_echo_on_session_setup():
    """平台建会话时必须向每个通道下发 stty -echo。

    这是广播模式不出现「命令显示两遍」的前提条件：前端做本地回显，
    远端必须闭嘴回显，否则两边各显示一次。
    """

    async def scenario(port_map):
        await open_and_broadcast(port_map, command="df -Th")
        for i in sorted(port_map):
            node = f"web-0{i}"
            received = RECEIVED.get(node, [])
            bootstrap = [c for c in received if c.startswith("stty")]
            assert bootstrap, f"{node} 未收到 stty 引导命令：{received}"
            assert any("-echo" in c for c in bootstrap), (
                f"{node} 的 stty 命令未关闭回显：{bootstrap}"
            )

    asyncio.run(run_with_servers(scenario))


def test_no_duplicate_echo_when_remote_echo_off():
    """远端回显关闭时，命令文本不应出现在远端输出里（避免重复显示）。

    若平台未正确关闭远端回显，用户会看到「df -Th」显示两遍：
    一次来自服务器回显，一次来自前端本地回显。
    """

    async def scenario(port_map):
        _, sink, _ = await open_and_broadcast(port_map, command="df -Th")
        for sid in (1, 2, 3, 4):
            out = sink.outputs.get(sid, "")
            assert out.count("df -Th") == 0, (
                f"server {sid} 输出中出现命令回显，会造成重复显示：{out!r}"
            )
            # 结果本身必须完整返回
            assert "Filesystem" in out
            assert "/dev/sda1" in out

    asyncio.run(run_with_servers(scenario))


def test_broadcast_latency_is_low():
    """广播需足够快，否则做不到「四台视觉同步」。并行写入是关键。"""

    async def scenario(port_map):
        from app.services.ssh_pool import pool

        hub = TerminalHub()
        sink = CollectingSink()
        servers = [FakeServer(i, f"web-0{i}", port_map[i]) for i in sorted(port_map)]
        session = await hub.create_session(servers, sink, mode="mirror")
        assert len(session.ready_slots) == 4

        started = time.monotonic()
        await hub.broadcast_input(session.session_id, "df -Th\r", operator=1)
        elapsed = time.monotonic() - started

        await hub.close_all()
        await pool.close_all()
        print(f"\n广播到 4 台耗时：{elapsed * 1000:.1f}ms")
        assert elapsed < 0.5, f"广播耗时 {elapsed:.3f}s，过慢"

    asyncio.run(run_with_servers(scenario))


def test_unknown_command_returns_shell_error():
    """错误命令的输出也要正确回传，便于用户判断。"""

    async def scenario(port_map):
        _, sink, _ = await open_and_broadcast(port_map, command="nosuchcmd --foo")
        for sid in (1, 2, 3, 4):
            out = sink.outputs.get(sid, "")
            assert "command not found" in out, f"server {sid} 未返回错误：{out!r}"

    asyncio.run(run_with_servers(scenario))


def test_single_mode_does_not_broadcast():
    """单机模式只写目标终端，其他终端不应收到内容。"""

    async def scenario(port_map):
        from app.services.ssh_pool import pool

        hub = TerminalHub()
        sink = CollectingSink()
        servers = [FakeServer(i, f"web-0{i}", port_map[i]) for i in sorted(port_map)]
        session = await hub.create_session(servers, sink, mode="single")
        assert len(session.ready_slots) == 4

        await hub.send_to(session.session_id, 2, "hostname\r")
        await asyncio.sleep(1.5)

        out2 = sink.outputs.get(2, "")
        assert "web-02" in out2, f"目标终端未执行：{out2!r}"

        for sid in (1, 3, 4):
            assert "web-02" not in sink.outputs.get(sid, ""), (
                f"单机模式下 server {sid} 不应收到内容"
            )

        await hub.close_all()
        await pool.close_all()

    asyncio.run(run_with_servers(scenario))


# ─────────────────────────────────────────────── SFTP 文件传输


def test_sftp_list_dir():
    """SFTP 列目录，且过滤掉 . 与 .."""

    async def scenario(port_map):
        from app.services.ssh_pool import pool

        executor = await pool.get(FakeServer(1, "web-01", port_map[1]))
        entries = await executor.list_dir(".")
        names = {e.name for e in entries}

        assert "sample.txt" in names, f"未列出示例文件：{names}"
        assert "logs" in names
        assert "." not in names and ".." not in names, "不应返回 . 与 .."
        assert next(e for e in entries if e.name == "logs").is_dir

        await pool.close_all()

    asyncio.run(run_with_servers(scenario, with_sftp=True))


def test_sftp_upload_and_download_roundtrip():
    """上传后能读回同样的内容 —— 验证文件传输链路。"""

    async def scenario(port_map):
        from app.services.ssh_pool import pool

        executor = await pool.get(FakeServer(1, "web-01", port_map[1]))

        src = _scratch_dir("sftp-src") / "payload.txt"
        content = "AI 运维平台上传测试\nline2\n" * 20
        # 用 write_bytes 而非 write_text：Windows 上 write_text 会把 \n 翻译成
        # \r\n，导致磁盘字节数与内存字符串长度不一致，断言会误判。
        src.write_bytes(content.encode("utf-8"))

        await executor.upload(str(src), "/uploaded.txt")

        # 列目录应能看到刚上传的文件
        entries = await executor.list_dir(".")
        uploaded = next((e for e in entries if e.name == "uploaded.txt"), None)
        assert uploaded is not None, "上传后目录中未出现该文件"
        assert uploaded.size == src.stat().st_size, "上传后文件大小与源文件不一致"

        # 下载回来内容必须一致
        dst = _scratch_dir("sftp-dst") / "back.txt"
        await executor.download("/uploaded.txt", str(dst))
        assert dst.read_bytes() == src.read_bytes(), "下载内容与上传不一致"

        await pool.close_all()

    asyncio.run(run_with_servers(scenario, with_sftp=True))


def test_sftp_upload_parallel_to_multiple_servers():
    """一次上传到多台服务器（需求中的批量上传场景）。"""

    async def scenario(port_map):
        from app.services.ssh_pool import pool

        src = _scratch_dir("sftp-src") / "multi.txt"
        src.write_bytes("broadcast upload".encode("utf-8"))

        async def push(sid: int) -> bool:
            executor = await pool.get(FakeServer(sid, f"web-0{sid}", port_map[sid]))
            await executor.upload(str(src), "/multi.txt")
            entries = await executor.list_dir(".")
            return any(e.name == "multi.txt" for e in entries)

        results = await asyncio.gather(*[push(i) for i in sorted(port_map)])
        assert all(results), f"并非所有服务器都收到文件：{results}"

        await pool.close_all()

    asyncio.run(run_with_servers(scenario, with_sftp=True))
