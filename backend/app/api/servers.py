"""服务器资产与分组管理。"""

from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.crypto import encrypt_secret
from app.core.security import CurrentUser, OperatorUser
from app.db import get_db
from app.models import Server, ServerGroup
from app.schemas import (
    ConnectionTestResult,
    ServerGroupIn,
    ServerGroupOut,
    ServerIn,
    ServerOut,
    ServerUpdate,
)
from app.services.ssh_pool import pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/servers", tags=["服务器"])


def _to_out(server: Server) -> ServerOut:
    data = ServerOut.model_validate(server)
    data.has_secret = bool(server.secret_ciphertext)
    return data


# --------------------------------------------------------------------------- 分组


@router.get("/groups", response_model=list[ServerGroupOut], summary="分组列表")
def list_groups(
    _: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> list[ServerGroupOut]:
    return [ServerGroupOut.model_validate(g) for g in db.query(ServerGroup).all()]


@router.post("/groups", response_model=ServerGroupOut, summary="创建分组")
def create_group(
    payload: ServerGroupIn,
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
) -> ServerGroupOut:
    if db.query(ServerGroup).filter(ServerGroup.name == payload.name).first():
        raise HTTPException(status_code=400, detail="分组名已存在")
    group = ServerGroup(name=payload.name, description=payload.description)
    db.add(group)
    db.commit()
    db.refresh(group)
    return ServerGroupOut.model_validate(group)


@router.delete("/groups/{group_id}", summary="删除分组")
def delete_group(
    group_id: int, _: OperatorUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    group = db.get(ServerGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="分组不存在")
    db.delete(group)
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- 服务器


@router.get("", response_model=list[ServerOut], summary="服务器列表")
def list_servers(
    _: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    group_id: int | None = None,
) -> list[ServerOut]:
    query = db.query(Server)
    if group_id is not None:
        query = query.filter(Server.group_id == group_id)
    return [_to_out(s) for s in query.order_by(Server.name).all()]


@router.post("", response_model=ServerOut, summary="新增服务器")
def create_server(
    payload: ServerIn,
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
) -> ServerOut:
    exists = (
        db.query(Server)
        .filter(
            Server.host == payload.host,
            Server.port == payload.port,
            Server.username == payload.username,
        )
        .first()
    )
    if exists:
        raise HTTPException(status_code=400, detail="该主机+端口+用户名的组合已存在")

    server = Server(
        name=payload.name,
        host=payload.host,
        port=payload.port,
        username=payload.username,
        protocol=payload.protocol,
        auth_type=payload.auth_type,
        group_id=payload.group_id,
        tags=payload.tags or {},
    )
    if payload.secret:
        ciphertext, nonce = encrypt_secret(payload.secret)
        server.secret_ciphertext = ciphertext
        server.secret_nonce = nonce

    db.add(server)
    db.commit()
    db.refresh(server)
    return _to_out(server)


@router.get("/{server_id}", response_model=ServerOut, summary="服务器详情")
def get_server(
    server_id: int, _: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> ServerOut:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="服务器不存在")
    return _to_out(server)


@router.patch("/{server_id}", response_model=ServerOut, summary="修改服务器")
def update_server(
    server_id: int,
    payload: ServerUpdate,
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
) -> ServerOut:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="服务器不存在")

    data = payload.model_dump(exclude_unset=True)
    secret = data.pop("secret", None)
    for field, value in data.items():
        setattr(server, field, value)
    if secret:
        ciphertext, nonce = encrypt_secret(secret)
        server.secret_ciphertext = ciphertext
        server.secret_nonce = nonce
        # 凭据变更后必须重建连接
        await_pool_release(server_id)

    db.add(server)
    db.commit()
    db.refresh(server)
    return _to_out(server)


def await_pool_release(server_id: int) -> None:
    """凭据变更后标记连接失效。

    这里不直接 await 关闭（在同步路由中），而是把连接从池中移除，
    下次使用时会自动重建。
    """
    pool._executors.pop(server_id, None)


@router.delete("/{server_id}", summary="删除服务器")
def delete_server(
    server_id: int, _: OperatorUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="服务器不存在")
    await_pool_release(server_id)
    db.delete(server)
    db.commit()
    return {"ok": True}


@router.post(
    "/{server_id}/test", response_model=ConnectionTestResult, summary="测试连通性"
)
async def test_connection(
    server_id: int, _: OperatorUser, db: Annotated[Session, Depends(get_db)]
) -> ConnectionTestResult:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="服务器不存在")

    started = time.monotonic()
    try:
        executor = await pool.get(server)
        result = await executor.run("echo aiops-ok", timeout=15)
        duration = int((time.monotonic() - started) * 1000)
        ok = result.ok and "aiops-ok" in result.stdout
        # 记录状态
        server.status = "online" if ok else "offline"
        server.last_error = "" if ok else (result.stderr or "测试命令未返回预期结果")[:500]
        from datetime import datetime, timezone

        server.last_seen = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(server)
        db.commit()
        return ConnectionTestResult(
            server_id=server_id,
            ok=ok,
            message="连接成功" if ok else server.last_error,
            duration_ms=duration,
        )
    except Exception as exc:
        duration = int((time.monotonic() - started) * 1000)
        server.status = "offline"
        server.last_error = str(exc)[:500]
        db.add(server)
        db.commit()
        return ConnectionTestResult(
            server_id=server_id, ok=False, message=str(exc), duration_ms=duration
        )


@router.post("/{server_id}/diagnose", summary="分阶段诊断连接")
async def diagnose(
    server_id: int, _: OperatorUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    """逐阶段诊断到服务器的 SSH 连接。

    与 /test 的区别：/test 只给「成功/失败」，本接口返回每一步的结论，
    便于定位到底是网络、算法、认证还是会话阶段出问题。
    """
    from app.core.crypto import decrypt_secret
    from app.services.server_ref import to_ref
    from app.services.ssh_pool import diagnose_server

    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="服务器不存在")

    password = None
    if server.secret_ciphertext and server.secret_nonce:
        try:
            password = decrypt_secret(server.secret_ciphertext, server.secret_nonce)
        except Exception as exc:
            return {
                "ok": False,
                "target": f"{server.host}:{server.port}",
                "steps": [
                    {
                        "step": "0. 凭据解密",
                        "ok": False,
                        "detail": f"无法解密已保存的凭据：{exc}。"
                        f"主密钥可能已更换，请重新填写密码或私钥。",
                    }
                ],
            }

    if password is None:
        return {
            "ok": False,
            "target": f"{server.host}:{server.port}",
            "steps": [
                {
                    "step": "0. 凭据检查",
                    "ok": False,
                    "detail": "该服务器没有保存任何密码或私钥，无法认证。"
                    "请在编辑中重新填写凭据。",
                }
            ],
        }

    ref = to_ref(server)
    return await diagnose_server(ref, password)


@router.post("/refresh-status", summary="刷新全部服务器状态")
async def refresh_status(
    _: OperatorUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    """并发测试所有服务器连通性。"""
    import asyncio

    servers = db.query(Server).all()
    results = await asyncio.gather(
        *[_probe(s) for s in servers], return_exceptions=True
    )
    online = sum(1 for r in results if r is True)
    return {"total": len(servers), "online": online}


async def _probe(server: Server) -> bool:
    """探测单台服务器，返回是否在线。使用独立会话避免并发写冲突。"""
    from datetime import datetime, timezone

    from app.db import SessionLocal

    status = "offline"
    error = ""
    try:
        executor = await pool.get(server)
        result = await executor.run("echo aiops-ok", timeout=10)
        if result.ok and "aiops-ok" in result.stdout:
            status = "online"
        else:
            error = (result.stderr or "探测未返回预期结果")[:500]
    except Exception as exc:
        error = str(exc)[:500]

    db = SessionLocal()
    try:
        fresh = db.get(Server, server.id)
        if fresh is not None:
            fresh.status = status
            fresh.last_error = error
            if status == "online":
                fresh.last_seen = datetime.now(timezone.utc).replace(tzinfo=None)
            db.add(fresh)
            db.commit()
    finally:
        db.close()
    return status == "online"
