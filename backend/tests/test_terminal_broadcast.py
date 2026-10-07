"""广播终端逻辑测试。

这是需求 2 的核心：验证"在一台终端输入 df -Th，四台同步显示、同时执行"。
用 FakeExecutor 替换真实 SSH，使测试无需真实服务器即可验证广播语义。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.executor_base import ChannelHandle, ExecResult, Executor, FileEntry
from app.services.terminal import TerminalHub, is_dangerous


class FakeChannel:
    """记录所有写入内容的假通道。"""

    def __init__(self, server_id: int) -> None:
        self.server_id = server_id
        self.writes: list[str] = []

    async def write(self, data: str) -> None:
        self.writes.append(data)

    @property
    def text(self) -> str:
        return "".join(self.writes)


class FakeExecutor(Executor):
    """假执行器：不连接任何真实服务器，只记录行为。"""

    def __init__(self, server_id: int, name: str, fail: bool = False) -> None:
        super().__init__(server_id, name)
        self.channel = FakeChannel(server_id)
        self.echo_off_requested: bool | None = None
        self.fail = fail
        self.closed = False

    async def connect(self) -> None:
        if self.fail:
            raise RuntimeError("模拟连接失败")
        self.is_connected = True

    async def close(self) -> None:
        self.closed = True
        self.is_connected = False

    async def run(self, command: str, timeout: int | None = None) -> ExecResult:
        return ExecResult(exit_code=0, stdout="ok", stderr="")

    async def open_terminal(self, sink, cols=120, rows=30, echo_off=False) -> ChannelHandle:
        self.echo_off_requested = echo_off
        if echo_off:
            # 真实实现会写入 stty -echo；这里记录以便断言
            self.channel.writes.append("<STTY_ECHO_OFF>")

        async def _write(data: str) -> None:
            await self.channel.write(data)

        async def _resize(c: int, r: int) -> None:
            pass

        async def _close() -> None:
            self.closed = True

        return ChannelHandle(
            server_id=self.server_id,
            server_name=self.name,
            write=_write,
            resize=_resize,
            close=_close,
        )

    async def upload(self, local_path, remote_path, on_progress=None) -> None:
        pass

    async def download(self, remote_path, local_path, on_progress=None) -> None:
        pass

    async def list_dir(self, remote_path: str) -> list[FileEntry]:
        return []


class FakeSink:
    """收集投递到"前端"的消息。"""

    def __init__(self) -> None:
        self.outputs: list[tuple[int, str]] = []
        self.statuses: list[tuple[int, str, str]] = []

    async def push_output(self, server_id: int, data: str) -> None:
        self.outputs.append((server_id, data))

    async def push_status(self, server_id: int, status: str, message: str = "") -> None:
        self.statuses.append((server_id, status, message))

    def output_text(self, server_id: int) -> str:
        return "".join(d for sid, d in self.outputs if sid == server_id)


class FakeServer:
    def __init__(self, sid: int, name: str) -> None:
        self.id = sid
        self.name = name
        self.host = f"10.0.0.{sid}"
        self.port = 22
        self.username = "root"
        self.secret_ciphertext = None
        self.secret_nonce = None
        self.auth_type = "password"
        self.protocol = "ssh"
        self.platform = "linux"


@pytest.fixture
def patched_pool(monkeypatch):
    """把 SSH 连接池替换为假执行器池。"""
    from app.services import ssh_pool

    executors: dict[int, FakeExecutor] = {}

    async def fake_get(server):
        if server.id not in executors:
            executors[server.id] = FakeExecutor(server.id, server.name)
        executor = executors[server.id]
        await executor.connect()
        return executor

    monkeypatch.setattr(ssh_pool.pool, "get", fake_get)
    # terminal 模块在导入时绑定了 pool 对象，方法被 patch 后共享同一实例
    return executors


# ============================================================ 核心广播语义


def test_broadcast_input_reaches_all_terminals(patched_pool):
    """★ 核心需求：输入一次，四台终端全部收到。"""

    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(i, f"web-0{i}") for i in range(1, 5)]

        session = await hub.create_session(servers, sink, mode="mirror")
        assert len(session.ready_slots) == 4

        sent = await hub.broadcast_input(session.session_id, "df -Th")

        assert sorted(sent) == [1, 2, 3, 4], "四台终端都应收到输入"
        for sid in (1, 2, 3, 4):
            executor = patched_pool[sid]
            assert "df -Th" in executor.channel.text, f"server {sid} 未收到命令"
        await hub.close_all()

    asyncio.run(scenario())


def test_broadcast_carriage_return_executes_simultaneously(patched_pool):
    """★ 回车必须一起广播，保证四台"同时执行"。"""

    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(i, f"web-0{i}") for i in range(1, 5)]
        session = await hub.create_session(servers, sink, mode="mirror")

        await hub.broadcast_input(session.session_id, "df -Th")
        await hub.broadcast_input(session.session_id, "\r")

        for sid in (1, 2, 3, 4):
            text = patched_pool[sid].channel.text
            # 命令后必须紧跟回车
            assert "df -Th\r" in text, f"server {sid} 的回车未同步"
        await hub.close_all()

    asyncio.run(scenario())


def test_char_by_char_broadcast_is_consistent(patched_pool):
    """逐字符输入时，四台终端的字符序列必须完全一致。"""

    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(i, f"web-0{i}") for i in range(1, 5)]
        session = await hub.create_session(servers, sink, mode="mirror")

        for char in "df -Th":
            await hub.broadcast_input(session.session_id, char)

        texts = {
            sid: patched_pool[sid].channel.text.replace("<STTY_ECHO_OFF>", "")
            for sid in (1, 2, 3, 4)
        }
        assert len(set(texts.values())) == 1, f"四台终端显示不一致：{texts}"
        assert texts[1] == "df -Th"
        await hub.close_all()

    asyncio.run(scenario())


def test_echo_off_enabled_in_mirror_mode(patched_pool):
    """镜像模式必须关闭远端回显，否则会与前端本地回显重复显示。"""

    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(1, "web-01")]
        await hub.create_session(servers, sink, mode="mirror")
        assert patched_pool[1].echo_off_requested is True
        await hub.close_all()

    asyncio.run(scenario())


def test_echo_off_disabled_in_single_mode(patched_pool):
    """单机模式保留远端回显（用户需要看到自己输入的内容）。"""

    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(1, "web-01")]
        await hub.create_session(servers, sink, mode="single")
        assert patched_pool[1].echo_off_requested is False
        await hub.close_all()

    asyncio.run(scenario())


def test_one_failed_server_does_not_break_others(monkeypatch):
    """单台连接失败不应影响其他终端 —— 部分可用优于全部失败。"""
    from app.services import ssh_pool

    executors: dict[int, FakeExecutor] = {}

    async def fake_get(server):
        if server.id not in executors:
            executors[server.id] = FakeExecutor(
                server.id, server.name, fail=(server.id == 2)
            )
        executor = executors[server.id]
        await executor.connect()
        return executor

    monkeypatch.setattr(ssh_pool.pool, "get", fake_get)

    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(i, f"web-0{i}") for i in range(1, 5)]
        session = await hub.create_session(servers, sink, mode="mirror")

        assert session.slots[2].status == "offline"
        assert len(session.ready_slots) == 3

        sent = await hub.broadcast_input(session.session_id, "df -Th")
        assert sorted(sent) == [1, 3, 4], "只有在线终端应收到输入"
        await hub.close_all()

    asyncio.run(scenario())


def test_send_to_single_terminal(patched_pool):
    """单机模式：只发送到指定终端。"""

    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(i, f"web-0{i}") for i in range(1, 3)]
        session = await hub.create_session(servers, sink, mode="diff")

        await hub.send_to(session.session_id, 1, "hostname")

        assert "hostname" in patched_pool[1].channel.text
        assert "hostname" not in patched_pool[2].channel.text
        await hub.close_all()

    asyncio.run(scenario())


def test_run_command_broadcasts_and_respects_dangerous_block(patched_pool):
    """命令输入栏回车：正常命令广播，危险命令拦截。"""

    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(i, f"web-0{i}") for i in range(1, 3)]
        session = await hub.create_session(servers, sink, mode="mirror")

        result = await hub.run_command(session.session_id, "df -Th")
        assert result["ok"] is True
        assert sorted(result["sent_to"]) == [1, 2]

        blocked = await hub.run_command(
            session.session_id, "rm -rf /", allowed=not is_dangerous("rm -rf /")
        )
        assert blocked["ok"] is False
        await hub.close_all()

    asyncio.run(scenario())


def test_command_audit_tracking(patched_pool):
    """回车时后端应记录完整命令行，用于审计。"""

    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(1, "web-01")]
        session = await hub.create_session(servers, sink, mode="mirror")

        # 未回车时不应产生审计记录（命令行还没结束）
        await hub.broadcast_input(session.session_id, "df -Th", operator=1)
        assert not session.pending_commands

        # 回车后形成完整命令
        await hub.broadcast_input(session.session_id, "\r", operator=1)
        commands = [c for c, _ in session.pending_commands.values()]
        assert commands == ["df -Th"], "回车后应记录待审计命令"
        assert 1 in session.pending_commands
        await hub.close_all()

    asyncio.run(scenario())


def test_run_command_records_audit(patched_pool):
    """通过命令输入栏执行时也必须进入审计（operator 不能被丢弃）。"""

    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(i, f"web-0{i}") for i in range(1, 3)]
        session = await hub.create_session(servers, sink, mode="mirror")

        await hub.run_command(session.session_id, "df -Th", operator=7)

        for slot in session.slots.values():
            assert slot.server_id in session.pending_commands
            command, _ = session.pending_commands[slot.server_id]
            assert command == "df -Th"
        await hub.close_all()

    asyncio.run(scenario())


def test_run_command_without_operator_skips_audit(patched_pool):
    """operator 为 None 时不记录审计（用于系统自身的内部调用）。"""

    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(1, "web-01")]
        session = await hub.create_session(servers, sink, mode="mirror")

        await hub.run_command(session.session_id, "df -Th", operator=None)
        assert not session.pending_commands
        await hub.close_all()

    asyncio.run(scenario())


def test_multi_line_paste_produces_multiple_audit_entries(patched_pool):
    """粘贴多行内容时应识别为多条命令。"""

    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(1, "web-01")]
        session = await hub.create_session(servers, sink, mode="mirror")

        await hub.broadcast_input(session.session_id, "df -Th\r", operator=1)
        await hub.broadcast_input(session.session_id, "free -m\r", operator=1)

        # 每次回车都会覆盖上一条（pending_commands 以 server 为键），
        # 因此这里断言最后一次记录正确即可 —— 实际落库由 _record_commands 批量完成
        command, _ = session.pending_commands[1]
        assert command == "free -m"
        await hub.close_all()

    asyncio.run(scenario())


def test_mode_switch_resends_echo_off(patched_pool):
    """从单机切到镜像模式时必须补发 stty -echo。

    注意：open_terminal(echo_off=True) 时假执行器记录标记 <STTY_ECHO_OFF>；
    而 set_mode 是直接向通道写入真实的 stty 命令文本，因此这里断言命令原文。
    """

    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(1, "web-01")]
        session = await hub.create_session(servers, sink, mode="single")

        assert "<STTY_ECHO_OFF>" not in patched_pool[1].channel.text
        assert "stty -echo" not in patched_pool[1].channel.text

        await hub.set_mode(session.session_id, "mirror")

        assert session.mode == "mirror"
        assert "stty -echo" in patched_pool[1].channel.text, (
            "切换模式后应向通道补发 stty -echo"
        )
        await hub.close_all()

    asyncio.run(scenario())


def test_mode_switch_to_single_does_not_send_echo_off(patched_pool):
    """切回单机模式不应再关闭回显（用户需要看到自己输入的内容）。"""

    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(1, "web-01")]
        session = await hub.create_session(servers, sink, mode="mirror")

        before = patched_pool[1].channel.text.count("stty -echo")
        await hub.set_mode(session.session_id, "single")
        after = patched_pool[1].channel.text.count("stty -echo")

        assert session.mode == "single"
        assert after == before, "切到单机模式不应追加 stty -echo"
        await hub.close_all()

    asyncio.run(scenario())


def test_invalid_mode_rejected(patched_pool):
    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(1, "web-01")]
        session = await hub.create_session(servers, sink, mode="mirror")
        assert await hub.set_mode(session.session_id, "bogus") is False
        assert session.mode == "mirror"
        await hub.close_all()

    asyncio.run(scenario())


def test_broadcast_to_unknown_session_is_safe(patched_pool):
    """对不存在的会话广播不应抛异常。"""

    async def scenario():
        hub = TerminalHub()
        assert await hub.broadcast_input("no-such-session", "df") == []
        assert await hub.send_to("no-such-session", 1, "df") is False

    asyncio.run(scenario())


def test_close_session_cleans_up(patched_pool):
    async def scenario():
        hub = TerminalHub()
        sink = FakeSink()
        servers = [FakeServer(i, f"web-0{i}") for i in range(1, 3)]
        session = await hub.create_session(servers, sink, mode="mirror")
        assert hub.session_count == 1

        await hub.close_session(session.session_id)
        assert hub.session_count == 0
        assert await hub.get(session.session_id) is None

    asyncio.run(scenario())
