"""多终端广播会话。

这是需求 2 的核心实现：在终端 A 输入 `df -Th`，B/C/D 实时同步显示，
按回车时四台同时执行。

关键设计（详见 docs/ARCHITECTURE.md §6.3）：

1. **本地回显 + 广播写入**
   前端收到按键后立即在所有面板本地渲染（零延迟、视觉完全一致），
   同时异步把字符广播到后端并行写入各 channel。

2. **关闭远端回显（stty -echo）**
   若依赖远端回显，四个终端会因网络延迟不同而显示进度不一，
   且遇到 vim/stty -echo 的机器会直接错位。因此在建立会话时关闭远端回显，
   使 reader 读到的都是纯命令输出。

3. **并行写入，不回等 ack**
   用 asyncio.gather 并行写入所有 channel，保证四台在同一事件循环 tick 内
   收到字符；回车作为普通输入一起广播，实现"同时执行"。

4. **命令边界识别**
   后端维护行缓冲，遇到 \r 或 \n 时把整行命令记入审计表。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from app.services.executor_base import ChannelHandle, OutputSink
from app.services.ssh_pool import pool

logger = logging.getLogger(__name__)

# 危险命令黑名单：命中后需人工二次确认
DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/\s*$",
    r"rm\s+-rf\s+/\*",
    r"mkfs\.\w+",
    r"dd\s+.*of=/dev/[sh]d[a-z]",
    r":\(\)\s*\{.*\};\s*:",          # fork 炸弹
    r">\s*/dev/[sh]d[a-z]",
    r"chmod\s+-R\s+777\s+/\s*$",
    r"shutdown|halt|poweroff|reboot",
    r"init\s+0",
]

_DANGEROUS_RE = [re.compile(p) for p in DANGEROUS_PATTERNS]


def is_dangerous(command: str) -> bool:
    cmd = command.strip()
    return any(rx.search(cmd) for rx in _DANGEROUS_RE)


@dataclass
class ServerSlot:
    """会话中一台服务器的状态。"""

    server_id: int
    name: str
    handle: ChannelHandle | None = None
    status: str = "connecting"     # connecting | ready | offline | closed
    error: str = ""
    line_buffer: str = ""


@dataclass
class BroadcastSession:
    """一个多终端会话。"""

    session_id: str
    mode: str = "mirror"           # mirror | diff | single | template
    slots: dict[int, ServerSlot] = field(default_factory=dict)
    sink: OutputSink | None = None
    operator: int | None = None
    created_at: float = field(default_factory=time.time)
    # 记录每条命令的执行时间，用于审计
    pending_commands: dict[int, tuple[str, float]] = field(default_factory=dict)

    @property
    def ready_slots(self) -> list[ServerSlot]:
        return [s for s in self.slots.values() if s.status == "ready" and s.handle]

    @property
    def server_ids(self) -> list[int]:
        return list(self.slots.keys())


class TerminalHub:
    """终端会话注册中心。"""

    def __init__(self) -> None:
        self._sessions: dict[str, BroadcastSession] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- 会话生命周期

    async def create_session(
        self,
        servers: list,
        sink: OutputSink,
        mode: str = "mirror",
        cols: int = 120,
        rows: int = 30,
        operator: int | None = None,
    ) -> BroadcastSession:
        session = BroadcastSession(
            session_id=str(uuid.uuid4()),
            mode=mode,
            sink=sink,
            operator=operator,
        )
        for srv in servers:
            session.slots[srv.id] = ServerSlot(server_id=srv.id, name=srv.name)

        async with self._lock:
            self._sessions[session.session_id] = session

        await sink.push_status(0, "session_ready", session.session_id)

        # 并行建立所有连接：某台失败不影响其他台
        await asyncio.gather(
            *[
                self._connect_slot(session, srv, cols, rows)
                for srv in servers
            ],
            return_exceptions=True,
        )
        return session

    async def _connect_slot(self, session: BroadcastSession, srv, cols: int, rows: int) -> None:
        slot = session.slots[srv.id]
        try:
            executor = await pool.get(srv)
            # 广播模式下关闭远端回显；单机模式保留远端回显
            echo_off = session.mode in ("mirror", "template")
            handle = await executor.open_terminal(
                session.sink, cols=cols, rows=rows, echo_off=echo_off
            )
            slot.handle = handle
            slot.status = "ready"
            await session.sink.push_status(srv.id, "ready", "")
            logger.info("会话 %s：%s 已就绪", session.session_id[:8], srv.name)
        except Exception as exc:
            slot.status = "offline"
            slot.error = str(exc)
            logger.warning("会话 %s：%s 连接失败 - %s", session.session_id[:8], srv.name, exc)
            await session.sink.push_status(srv.id, "offline", str(exc))

    async def get(self, session_id: str) -> BroadcastSession | None:
        return self._sessions.get(session_id)

    async def close_session(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return
        for slot in session.slots.values():
            if slot.handle:
                with contextlib.suppress(Exception):
                    await slot.handle.close()

    # ---------------------------------------------------------------- 输入广播 ★

    async def broadcast_input(self, session_id: str, data: str,
                              operator: int | None = None) -> list[int]:
        """把输入广播到所有就绪的终端。

        返回成功写入的 server_id 列表。

        这是"同步输入"的实现：并行写入所有 channel，不回等远端 ack，
        因此四台服务器在同一事件循环 tick 内收到相同字符。
        """
        session = self._sessions.get(session_id)
        if session is None:
            return []

        targets = session.ready_slots
        if not targets:
            return []

        # 记录命令边界，用于审计
        if operator is not None:
            self._track_command(session, data, operator)

        async def _send(slot: ServerSlot) -> int | None:
            try:
                await slot.handle.write(data)
                return slot.server_id
            except Exception as exc:
                logger.warning("写入 %s 失败：%s", slot.name, exc)
                slot.status = "closed"
                if session.sink:
                    await session.sink.push_status(slot.server_id, "closed", str(exc))
                return None

        results = await asyncio.gather(*[_send(s) for s in targets], return_exceptions=True)
        return [r for r in results if isinstance(r, int)]

    async def send_to(self, session_id: str, server_id: int, data: str) -> bool:
        """只发送到指定终端（单机模式 / diff 模式）。"""
        session = self._sessions.get(session_id)
        if session is None:
            return False
        slot = session.slots.get(server_id)
        if slot is None or slot.handle is None:
            return False
        try:
            await slot.handle.write(data)
            return True
        except Exception as exc:
            logger.warning("写入 %s 失败：%s", slot.name, exc)
            return False

    async def run_command(
        self,
        session_id: str,
        command: str,
        server_ids: list[int] | None = None,
        allowed: bool = True,
        operator: int | None = None,
    ) -> dict:
        """直接执行命令（用于命令输入栏，回车即同时执行）。"""
        session = self._sessions.get(session_id)
        if session is None:
            return {"ok": False, "error": "会话不存在"}

        if not allowed:
            return {"ok": False, "error": "危险命令已被拦截，请人工确认后执行"}

        targets = [
            s for s in session.ready_slots
            if server_ids is None or s.server_id in server_ids
        ]
        if not targets:
            return {"ok": False, "error": "没有可用的终端"}

        # 广播命令文本本身，让四个屏幕都显示同一行。
        # 必须传 operator，否则该命令不会进入审计记录。
        payload = command + "\r"
        sent = await self.broadcast_input(session_id, payload, operator=operator)
        return {"ok": True, "sent_to": sent, "command": command}

    def _track_command(self, session: BroadcastSession, data: str, operator: int) -> None:
        """按行缓冲识别命令边界，回车时记录待审计命令。"""
        for slot in session.slots.values():
            slot.line_buffer += data
            while "\r" in slot.line_buffer or "\n" in slot.line_buffer:
                # 取第一个换行符之前的完整行
                idx_r = slot.line_buffer.find("\r")
                idx_n = slot.line_buffer.find("\n")
                candidates = [i for i in (idx_r, idx_n) if i >= 0]
                idx = min(candidates)
                line = slot.line_buffer[:idx]
                slot.line_buffer = slot.line_buffer[idx + 1:]
                line = line.strip()
                if line:
                    session.pending_commands[slot.server_id] = (line, time.time())

    async def resize(self, session_id: str, cols: int, rows: int) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        await asyncio.gather(
            *[
                s.handle.resize(cols, rows)
                for s in session.ready_slots
            ],
            return_exceptions=True,
        )

    async def set_mode(self, session_id: str, mode: str) -> bool:
        """切换同步模式。

        从单机模式切到广播模式时需要补发 stty -echo，
        否则远端回显会与前端本地回显叠加造成重复显示。
        """
        session = self._sessions.get(session_id)
        if session is None or mode not in ("mirror", "diff", "single", "template"):
            return False
        session.mode = mode
        if mode in ("mirror", "template"):
            await asyncio.gather(
                *[
                    s.handle.write("stty -echo 2>/dev/null; export PS1=''\n")
                    for s in session.ready_slots
                ],
                return_exceptions=True,
            )
        return True

    async def close_all(self) -> None:
        for session_id in list(self._sessions.keys()):
            await self.close_session(session_id)

    @property
    def session_count(self) -> int:
        return len(self._sessions)


# 全局单例
hub = TerminalHub()
