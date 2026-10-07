"""终端 WebSocket 协议实现。

协议（见 docs/ARCHITECTURE.md §6.6）：

Client → Server
  {"type":"session.create", "server_ids":[1,2,3,4], "mode":"mirror", "cols":120, "rows":30}
  {"type":"input",  "session_id":"...", "server_id":1, "data":"d"}
  {"type":"broadcast_input", "session_id":"...", "data":"df -Th"}   ← 核心
  {"type":"run", "session_id":"...", "command":"df -Th"}
  {"type":"resize", "session_id":"...", "cols":120, "rows":30}
  {"type":"mode.change", "session_id":"...", "mode":"diff"}
  {"type":"session.close", "session_id":"..."}

Server → Client
  {"type":"session.ready", "session_id":"..."}
  {"type":"output", "session_id":"...", "server_id":1, "data":"..."}
  {"type":"broadcast_ack", "session_id":"...", "server_ids":[1,2,3,4]}
  {"type":"server.status", "session_id":"...", "server_id":2, "status":"offline"}
  {"type":"error", "session_id":"...", "message":"..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from jose import JWTError, jwt

from app.config import settings
from app.core.security import ALGORITHM
from app.db import SessionLocal
from app.models import Server, TerminalSession, User
from app.services.executor_base import OutputSink
from app.services.server_ref import to_ref
from app.services.terminal import hub, is_dangerous

logger = logging.getLogger(__name__)
router = APIRouter(tags=["终端"])


class WebSocketSink(OutputSink):
    """把终端输出投递到 WebSocket。

    输出是高频事件，因此用队列解耦读取与发送：
    reader 任务只管读并塞队列，独立发送协程负责写出，
    避免远端输出速度快时阻塞 SSH 读取。
    """

    def __init__(self, websocket: WebSocket, session_id: str = "") -> None:
        self._ws = websocket
        self._session_id = session_id
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        self._sender: asyncio.Task | None = None
        self._closed = False

    def set_session(self, session_id: str) -> None:
        self._session_id = session_id

    def start(self) -> None:
        if self._sender is None:
            self._sender = asyncio.create_task(self._send_loop())

    async def _send_loop(self) -> None:
        while not self._closed:
            try:
                message = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self._ws.send_text(json.dumps(message, ensure_ascii=False))
            except Exception:
                break

    def _enqueue(self, message: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            # 队列满说明前端消费不过来，丢弃最旧的消息以保持实时性
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(message)
            except Exception:
                pass

    async def push_output(self, server_id: int, data: str) -> None:
        self._enqueue(
            {
                "type": "output",
                "session_id": self._session_id,
                "server_id": server_id,
                "data": data,
            }
        )

    async def push_status(self, server_id: int, status: str, message: str = "") -> None:
        self._enqueue(
            {
                "type": "server.status",
                "session_id": self._session_id,
                "server_id": server_id,
                "status": status,
                "message": message,
            }
        )

    async def push(self, message: dict[str, Any]) -> None:
        self._enqueue(message)

    async def aclose(self) -> None:
        self._closed = True
        if self._sender is not None:
            self._sender.cancel()
            try:
                await self._sender
            except (asyncio.CancelledError, Exception):
                pass
            self._sender = None


def _authenticate(token: str | None) -> User | None:
    """WebSocket 鉴权。

    浏览器 WebSocket 无法自定义 header，因此 token 通过 query 参数传递。
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError:
        return None
    if payload.get("type") != "access":
        return None
    db = SessionLocal()
    try:
        user = db.get(User, int(payload.get("sub", 0)))
        return user if user and user.is_active else None
    finally:
        db.close()


@router.websocket("/ws/terminal")
async def terminal_websocket(
    websocket: WebSocket,
    token: str | None = Query(default=None),
) -> None:
    user = _authenticate(token)
    if user is None:
        await websocket.close(code=4401, reason="未认证")
        return

    await websocket.accept()
    sink = WebSocketSink(websocket)
    sink.start()

    current_session_id: str | None = None
    audit_buffer: dict[int, str] = {}

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await sink.push({"type": "error", "message": "消息格式错误"})
                continue

            msg_type = message.get("type")

            # ---------------------------------------------------- 创建会话
            if msg_type == "session.create":
                server_ids = message.get("server_ids") or []
                mode = message.get("mode", "mirror")
                cols = int(message.get("cols", 120))
                rows = int(message.get("rows", 30))

                if not server_ids:
                    await sink.push({"type": "error", "message": "未选择服务器"})
                    continue
                if user.role == "viewer":
                    await sink.push({"type": "error", "message": "只读用户无法打开终端"})
                    continue

                db = SessionLocal()
                try:
                    server_rows = (
                        db.query(Server).filter(Server.id.in_(server_ids)).all()
                    )
                    # 必须在 Session 关闭前转成快照，否则后续访问 server.name
                    # 会抛 DetachedInstanceError（终端会话的生命周期远长于本请求）。
                    refs = [to_ref(s) for s in server_rows]

                    record = TerminalSession(
                        name=message.get("name", ""),
                        mode=mode,
                        server_ids=server_ids,
                        created_by=user.id,
                    )
                    db.add(record)
                    db.commit()
                    db.refresh(record)
                    session_record_id = record.id
                finally:
                    db.close()

                if not refs:
                    await sink.push({"type": "error", "message": "服务器不存在"})
                    continue

                sink.set_session(session_record_id)
                current_session_id = session_record_id

                session = await hub.create_session(
                    servers=refs,
                    sink=sink,
                    mode=mode,
                    cols=cols,
                    rows=rows,
                    operator=user.id,
                )
                await sink.push(
                    {
                        "type": "session.ready",
                        "session_id": session.session_id,
                        "db_session_id": session_record_id,
                        "mode": session.mode,
                        "servers": [
                            {
                                "id": s.server_id,
                                "name": s.name,
                                "status": s.status,
                                # 必须带上连接失败原因：否则前端只知道 offline，
                                # 用户就会遇到「连不上却不显示原因」。
                                "error": s.error,
                            }
                            for s in session.slots.values()
                        ],
                    }
                )

            # ---------------------------------------------------- 广播输入 ★
            elif msg_type == "broadcast_input":
                session_id = message.get("session_id")
                data = message.get("data", "")
                if not session_id:
                    continue
                sent = await hub.broadcast_input(
                    session_id, data, operator=user.id
                )
                # 逐字符广播时不必每次都回 ack，避免消息风暴；
                # 只在回车（命令提交）时回执，便于前端确认执行范围。
                if "\r" in data or "\n" in data:
                    await sink.push(
                        {
                            "type": "broadcast_ack",
                            "session_id": session_id,
                            "server_ids": sent,
                        }
                    )
                    await _record_commands(session_id, user.id)

            # ---------------------------------------------------- 单机输入
            elif msg_type == "input":
                session_id = message.get("session_id")
                server_id = message.get("server_id")
                data = message.get("data", "")
                if session_id and server_id is not None:
                    await hub.send_to(session_id, int(server_id), data)

            # ---------------------------------------------------- 直接执行命令
            elif msg_type == "run":
                session_id = message.get("session_id")
                command = (message.get("command") or "").strip()
                if not session_id or not command:
                    continue

                if is_dangerous(command):
                    await sink.push(
                        {
                            "type": "command.blocked",
                            "session_id": session_id,
                            "command": command,
                            "message": "该命令被判定为高危操作，已拦截。如确需执行请登录服务器手动操作。",
                        }
                    )
                    continue

                result = await hub.run_command(
                    session_id, command, message.get("server_ids")
                )
                await sink.push(
                    {
                        "type": "broadcast_ack",
                        "session_id": session_id,
                        "server_ids": result.get("sent_to", []),
                        "command": command,
                    }
                )
                await _record_commands(session_id, user.id)

            # ---------------------------------------------------- 调整尺寸
            elif msg_type == "resize":
                session_id = message.get("session_id")
                if session_id:
                    await hub.resize(
                        session_id,
                        int(message.get("cols", 120)),
                        int(message.get("rows", 30)),
                    )

            # ---------------------------------------------------- 切换模式
            elif msg_type == "mode.change":
                session_id = message.get("session_id")
                mode = message.get("mode", "mirror")
                if session_id and await hub.set_mode(session_id, mode):
                    await sink.push(
                        {"type": "mode.changed", "session_id": session_id, "mode": mode}
                    )

            # ---------------------------------------------------- 关闭会话
            elif msg_type == "session.close":
                session_id = message.get("session_id")
                if session_id:
                    await hub.close_session(session_id)
                    await sink.push({"type": "session.closed", "session_id": session_id})
                    current_session_id = None

            elif msg_type == "ping":
                await sink.push({"type": "pong"})

    except WebSocketDisconnect:
        logger.debug("终端 WebSocket 断开")
    except Exception:
        logger.exception("终端 WebSocket 异常")
    finally:
        if current_session_id:
            await hub.close_session(current_session_id)
        await sink.aclose()


async def _record_commands(session_id: str, operator: int) -> None:
    """把会话中累积的待审计命令落库。"""
    session = await hub.get(session_id)
    if session is None or not session.pending_commands:
        return

    from datetime import datetime, timezone

    from app.models import CommandAudit

    pending = dict(session.pending_commands)
    session.pending_commands.clear()

    db = SessionLocal()
    try:
        for server_id, (command, ts) in pending.items():
            db.add(
                CommandAudit(
                    session_id=session_id,
                    server_id=server_id,
                    command=command,
                    started_at=datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None),
                    operator=operator,
                )
            )
        db.commit()
    except Exception:
        logger.exception("记录命令审计失败")
    finally:
        db.close()
