"""告警规则与事件、AI 问答与根因分析、文件传输、审计。"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, OperatorUser
from app.db import get_db
from app.models import (
    AlertEvent,
    AlertRule,
    BackupRun,
    BackupTask,
    CommandAudit,
    DatabaseInstance,
    MetricSample,
    MetricScript,
    Server,
)
from app.schemas import (
    AIAskRequest,
    AIAskResponse,
    AlertEventOut,
    AlertRuleIn,
    AlertRuleOut,
    BackupTaskIn,
    BackupTaskOut,
    BackupTaskUpdate,
    DatabaseInstanceIn,
    DatabaseInstanceOut,
    DatabaseInstanceUpdate,
)
from app.services.alert_engine import acknowledge_alert
from app.services.executor_base import FileEntry
from app.services.ssh_pool import pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["告警与 AI"])


# ============================================================ 告警规则


@router.get("/alerts/rules", response_model=list[AlertRuleOut], summary="规则列表")
def list_rules(
    _: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> list[AlertRuleOut]:
    return [AlertRuleOut.model_validate(r) for r in db.query(AlertRule).all()]


@router.post("/alerts/rules", response_model=AlertRuleOut, summary="新建规则")
def create_rule(
    payload: AlertRuleIn,
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
) -> AlertRuleOut:
    if payload.operator not in (">", ">=", "<", "<=", "==", "!="):
        raise HTTPException(status_code=400, detail="运算符不合法")
    rule = AlertRule(**payload.model_dump())
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return AlertRuleOut.model_validate(rule)


@router.patch("/alerts/rules/{rule_id}", response_model=AlertRuleOut, summary="修改规则")
def update_rule(
    rule_id: int,
    payload: AlertRuleIn,
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
) -> AlertRuleOut:
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return AlertRuleOut.model_validate(rule)


@router.delete("/alerts/rules/{rule_id}", summary="删除规则")
def delete_rule(
    rule_id: int, _: OperatorUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    db.delete(rule)
    db.commit()
    return {"ok": True}


# ============================================================ 告警事件


@router.get("/alerts", response_model=list[AlertEventOut], summary="告警列表")
def list_alerts(
    _: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    status: str = Query(default="firing"),
    severity: str | None = None,
    limit: int = Query(default=100, le=500),
) -> list[AlertEventOut]:
    query = db.query(AlertEvent)
    if status != "all":
        query = query.filter(AlertEvent.status == status)
    if severity:
        query = query.filter(AlertEvent.severity == severity)

    events = query.order_by(AlertEvent.fired_at.desc()).limit(limit).all()
    names = {s.id: s.name for s in db.query(Server).all()}

    result = []
    for event in events:
        item = AlertEventOut.model_validate(event)
        item.server_name = names.get(event.server_id or -1, "")
        result.append(item)
    return result


@router.post("/alerts/{alert_id}/acknowledge", summary="确认告警")
def acknowledge(
    alert_id: int,
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser = None,
) -> dict:
    rule = db.query(AlertRule).join(
        AlertEvent, AlertEvent.rule_id == AlertRule.id
    ).filter(AlertEvent.id == alert_id).first()
    silence = rule.silence_min if rule else 5
    # CurrentUser 在同一签名中不能重复注入，这里直接取事件属主
    event = acknowledge_alert(db, alert_id, 0, silence)
    if event is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    return {"ok": True, "status": event.status}


@router.get("/alerts/{alert_id}", response_model=AlertEventOut, summary="告警详情")
def get_alert(
    alert_id: int, _: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> AlertEventOut:
    event = db.get(AlertEvent, alert_id)
    if event is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    item = AlertEventOut.model_validate(event)
    server = db.get(Server, event.server_id) if event.server_id else None
    item.server_name = server.name if server else ""
    return item


@router.post("/alerts/{alert_id}/analyze", summary="触发 AI 根因分析")
async def analyze(
    alert_id: int, _: OperatorUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    from app.services.ai.analyzer import analyze_alert

    if db.get(AlertEvent, alert_id) is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    result = await analyze_alert(alert_id)
    return result or {"error": "分析失败"}


# ============================================================ AI 问答


@router.post("/ai/ask", response_model=AIAskResponse, summary="运维问答")
async def ai_ask(payload: AIAskRequest, _: CurrentUser) -> AIAskResponse:
    from app.services.ai.copilot import ask
    from app.services.ai.provider import get_provider

    provider = get_provider()
    result = await ask(payload.question)
    return AIAskResponse(
        answer=result["answer"],
        tool_calls=result.get("tool_calls", []),
        model=result.get("model", ""),
        enabled=result.get("enabled", provider.enabled),
    )


@router.get("/ai/status", summary="AI 状态")
def ai_status(_: CurrentUser) -> dict:
    from app.services.ai.provider import get_provider

    provider = get_provider()
    return {
        "enabled": provider.enabled,
        "model": provider.model,
        "base_url": provider.base_url,
        "has_api_key": bool(provider.api_key),
    }


# ============================================================ 文件传输


@router.get("/files/list", summary="列出远端目录")
async def list_remote(
    _: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    server_id: int = Query(...),
    path: str = Query(default="."),
) -> dict:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="服务器不存在")
    try:
        executor = await pool.get(server)
        entries = await executor.list_dir(path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"读取目录失败：{exc}") from None

    return {
        "path": path,
        "entries": [
            {
                "name": e.name,
                "path": e.path,
                "is_dir": e.is_dir,
                "size": e.size,
                "mtime": e.mtime,
                "mode": e.mode,
            }
            for e in entries
        ],
    }


@router.post("/files/upload", summary="上传文件到多台服务器")
async def upload(
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
    server_ids: str = Form(..., description="逗号分隔"),
    remote_path: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    """并行上传到多台服务器。

    单台失败不影响其他台；返回每台的结果。
    """
    import asyncio

    try:
        ids = [int(x) for x in server_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="server_ids 格式错误") from None
    if not ids:
        raise HTTPException(status_code=400, detail="未选择服务器")

    # 先落盘到临时文件（SFTP 需要本地路径）
    suffix = os.path.splitext(file.filename or "")[1]
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="aiops_upload_")
    try:
        with os.fdopen(fd, "wb") as fh:
            while chunk := await file.read(1024 * 1024):
                fh.write(chunk)

        async def _upload_one(sid: int) -> dict:
            server = db.get(Server, sid)
            if server is None:
                return {"server_id": sid, "ok": False, "error": "服务器不存在"}
            try:
                executor = await pool.get(server)
                target = remote_path.rstrip("/") + "/" + (file.filename or "upload.bin")
                await executor.upload(tmp_path, target)
                return {
                    "server_id": sid,
                    "server_name": server.name,
                    "ok": True,
                    "path": target,
                    "size": os.path.getsize(tmp_path),
                }
            except Exception as exc:
                return {"server_id": sid, "server_name": server.name,
                        "ok": False, "error": str(exc)}

        results = await asyncio.gather(
            *[_upload_one(sid) for sid in ids], return_exceptions=True
        )
        cleaned = []
        for r in results:
            if isinstance(r, BaseException):
                cleaned.append({"ok": False, "error": str(r)})
            else:
                cleaned.append(r)

        succeeded = sum(1 for r in cleaned if r.get("ok"))
        return {
            "total": len(ids),
            "succeeded": succeeded,
            "failed": len(ids) - succeeded,
            "results": cleaned,
        }
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@router.get("/files/download", summary="从服务器下载文件")
async def download(
    _: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    server_id: int = Query(...),
    remote_path: str = Query(...),
) -> FileResponse:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="服务器不存在")

    filename = os.path.basename(remote_path) or "download.bin"
    tmp_dir = tempfile.mkdtemp(prefix="aiops_download_")
    local_path = os.path.join(tmp_dir, filename)
    try:
        executor = await pool.get(server)
        await executor.download(remote_path, local_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"下载失败：{exc}") from None

    return FileResponse(local_path, filename=filename)


# ============================================================ 审计


@router.get("/audit/commands", summary="命令审计日志")
def list_audit(
    _: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=200, le=1000),
    server_id: int | None = None,
) -> dict:
    query = db.query(CommandAudit)
    if server_id is not None:
        query = query.filter(CommandAudit.server_id == server_id)
    records = query.order_by(CommandAudit.started_at.desc()).limit(limit).all()
    names = {s.id: s.name for s in db.query(Server).all()}
    return {
        "records": [
            {
                "id": r.id,
                "session_id": r.session_id,
                "server_id": r.server_id,
                "server_name": names.get(r.server_id or -1, ""),
                "command": r.command,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "operator": r.operator,
            }
            for r in records
        ]
    }


# ============================================================ 数据库实例


def _require_exists(db, model, value, label: str) -> None:
    """校验外键引用存在。

    不做校验会直接抛 IntegrityError（500），
    前端只能看到「服务器错误」，无法判断是哪个 ID 填错了。
    """
    if value is None:
        return
    if db.get(model, value) is None:
        raise HTTPException(status_code=400, detail=f"{label}不存在（id={value}）")


def _validate_db_refs(db, data: dict) -> None:
    if "proxy_server_id" in data:
        _require_exists(db, Server, data["proxy_server_id"], "跳板服务器")


def _validate_backup_refs(db, data: dict) -> None:
    if "db_instance_id" in data:
        _require_exists(db, DatabaseInstance, data["db_instance_id"], "数据库实例")
    if "server_id" in data:
        _require_exists(db, Server, data["server_id"], "服务器")
    if "exec_server_id" in data:
        _require_exists(db, Server, data["exec_server_id"], "执行服务器")


def _db_out(instance: DatabaseInstance) -> DatabaseInstanceOut:
    """统一构造输出对象，并正确反映「是否已保存凭据」。

    漏掉 has_secret 会让界面永远显示「未保存」，
    用户会以为密码没存进去而反复重填。
    """
    data = DatabaseInstanceOut.model_validate(instance)
    data.has_secret = bool(instance.secret_ciphertext and instance.secret_nonce)
    return data


@router.get("/databases/overview", summary="数据库实例实时概况")
def database_overview(_: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> dict:
    """每个数据库实例的关键指标最新值（供大盘与列表页展示）。"""
    from app.services.alert_engine import compute_database_overview

    items = compute_database_overview(db)
    return {
        "total": len(items),
        "online": sum(1 for x in items if x["status"] == "online"),
        "databases": items,
    }


@router.get("/databases", response_model=list[DatabaseInstanceOut], summary="数据库实例列表")
def list_databases(
    _: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> list[DatabaseInstanceOut]:
    return [_db_out(d) for d in db.query(DatabaseInstance).all()]


@router.get("/databases/templates", summary="可选的采集脚本模板")
def db_templates(_: CurrentUser) -> dict:
    from app.services.db_collector import DB_TYPE_LABELS, DEFAULT_PORTS

    return {
        "db_types": [
            {"key": k, "label": v, "default_port": DEFAULT_PORTS.get(k, 0)}
            for k, v in DB_TYPE_LABELS.items()
        ]
    }


@router.get("/databases/{instance_id}", response_model=DatabaseInstanceOut,
            summary="数据库实例详情")
def get_database(
    instance_id: int, _: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> DatabaseInstanceOut:
    instance = db.get(DatabaseInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="实例不存在")
    return _db_out(instance)


@router.post("/databases", response_model=DatabaseInstanceOut, summary="新增数据库实例")
def create_database(
    payload: DatabaseInstanceIn,
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
) -> DatabaseInstanceOut:
    from app.core.crypto import encrypt_secret

    payload_data = payload.model_dump()
    _validate_db_refs(db, payload_data)

    instance = DatabaseInstance(
        name=payload.name,
        db_type=payload.db_type,
        host=payload.host,
        port=payload.port,
        username=payload.username,
        database_name=payload.database_name,
        collect_mode=payload.collect_mode,
        proxy_server_id=payload.proxy_server_id,
        proxy_host=payload.proxy_host,
        client_path=payload.client_path,
        enabled=payload.enabled,
    )
    if payload.secret:
        ciphertext, nonce = encrypt_secret(payload.secret)
        instance.secret_ciphertext = ciphertext
        instance.secret_nonce = nonce
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return _db_out(instance)


@router.patch("/databases/{instance_id}", response_model=DatabaseInstanceOut,
              summary="修改数据库实例")
def update_database(
    instance_id: int,
    payload: DatabaseInstanceUpdate,
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
) -> DatabaseInstanceOut:
    """修改数据库实例。只需传要改的字段；secret 留空表示不修改密码。"""
    from app.core.crypto import encrypt_secret

    instance = db.get(DatabaseInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="实例不存在")

    data = payload.model_dump(exclude_unset=True)
    _validate_db_refs(db, data)
    secret = data.pop("secret", None)
    for field, value in data.items():
        setattr(instance, field, value)
    if secret:
        ciphertext, nonce = encrypt_secret(secret)
        instance.secret_ciphertext = ciphertext
        instance.secret_nonce = nonce
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return _db_out(instance)


@router.delete("/databases/{instance_id}", summary="删除数据库实例")
def delete_database(
    instance_id: int, _: OperatorUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    instance = db.get(DatabaseInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="实例不存在")
    # 同时清理它的备份任务，避免留下悬空引用
    for task in db.query(BackupTask).filter(BackupTask.db_instance_id == instance_id).all():
        from app.services.backup import unregister_backup_task

        unregister_backup_task(task.id)
        db.delete(task)
    db.delete(instance)
    db.commit()
    return {"ok": True}


@router.post("/databases/{instance_id}/test", summary="测试数据库连接")
async def test_database(
    instance_id: int, _: OperatorUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    """真实连接数据库验证凭据与网络可达性。"""
    from app.core.crypto import decrypt_secret
    from app.services import db_collector

    instance = db.get(DatabaseInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="实例不存在")

    if not (instance.secret_ciphertext and instance.secret_nonce):
        return {"ok": False, "message": "该实例没有保存密码，无法认证"}

    try:
        password = decrypt_secret(instance.secret_ciphertext, instance.secret_nonce)
    except Exception as exc:
        return {"ok": False, "message": f"凭据解密失败：{exc}"}

    info = db_collector.DBConnectionInfo(
        instance_id=instance.id,
        name=instance.name,
        db_type=instance.db_type,
        host=instance.host,
        port=instance.port,
        username=instance.username,
        database_name=instance.database_name,
        password=password,
        proxy_host=instance.proxy_host or "",
        client_path=instance.client_path or "",
    )

    executor = None
    if instance.collect_mode == "ssh_proxy":
        if instance.proxy_server_id is None:
            return {"ok": False, "message": "采集方式为 SSH 跳板，但未指定跳板服务器"}
        proxy = db.get(Server, instance.proxy_server_id)
        if proxy is None:
            return {"ok": False, "message": "指定的跳板服务器不存在"}
        try:
            executor = await pool.get(proxy)
        except Exception as exc:
            return {"ok": False, "message": f"跳板服务器连接失败：{exc}"}

    ok, message, duration_ms = await db_collector.test_connection(info, executor=executor)

    instance.status = "online" if ok else "offline"
    instance.last_error = "" if ok else message[:500]
    db.add(instance)
    db.commit()

    return {"ok": ok, "message": message, "duration_ms": duration_ms}


@router.post("/databases/{instance_id}/collect", summary="立即采集一次数据库指标")
async def collect_database_now(
    instance_id: int, _: OperatorUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    """对指定实例跑一次所有匹配的 SQL 脚本，返回原始与解析结果。"""
    from app.core.crypto import decrypt_secret
    from app.services import db_collector
    from app.services.script_runner import parse_output

    instance = db.get(DatabaseInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="实例不存在")

    scripts = (
        db.query(MetricScript)
        .filter(
            MetricScript.runtime == "sql",
            MetricScript.db_type == instance.db_type,
        )
        .all()
    )
    if not scripts:
        return {
            "ok": False,
            "message": f"没有适用于 {instance.db_type} 的 SQL 采集脚本",
            "results": [],
        }

    password = None
    if instance.secret_ciphertext and instance.secret_nonce:
        try:
            password = decrypt_secret(instance.secret_ciphertext, instance.secret_nonce)
        except Exception as exc:
            return {"ok": False, "message": f"凭据解密失败：{exc}", "results": []}

    info = db_collector.DBConnectionInfo(
        instance_id=instance.id,
        name=instance.name,
        db_type=instance.db_type,
        host=instance.host,
        port=instance.port,
        username=instance.username,
        database_name=instance.database_name,
        password=password,
        proxy_host=instance.proxy_host or "",
        client_path=instance.client_path or "",
    )

    executor = None
    if instance.collect_mode == "ssh_proxy" and instance.proxy_server_id:
        proxy = db.get(Server, instance.proxy_server_id)
        if proxy is not None:
            try:
                executor = await pool.get(proxy)
            except Exception as exc:
                return {"ok": False, "message": f"跳板服务器连接失败：{exc}", "results": []}

    results = []
    stored = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for script in scripts:
        started = time.monotonic()
        try:
            if executor is not None:
                metrics = await db_collector.collect_via_proxy(
                    executor, info, script.body, script.timeout_sec
                )
            else:
                metrics = await db_collector.collect_direct(
                    info, script.body, script.timeout_sec
                )
            # 「立即采集」必须真正落库，否则用户点了却在大盘上看不到数据
            for m in metrics:
                db.add(
                    MetricSample(
                        server_id=None,
                        db_instance_id=instance.id,
                        metric_key=m.key,
                        ts=now,
                        value=m.value,
                        labels=m.labels or None,
                    )
                )
            stored += len(metrics)

            results.append(
                {
                    "script": script.name,
                    "ok": True,
                    "metrics": [
                        {"key": m.key, "value": m.value, "labels": m.labels} for m in metrics
                    ],
                    "count": len(metrics),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "script": script.name,
                    "ok": False,
                    "error": str(exc)[:500],
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )

    ok = any(r["ok"] for r in results)
    instance.status = "online" if ok else "offline"
    if not ok:
        instance.last_error = next(
            (r.get("error", "") for r in results if not r["ok"]), ""
        )[:500]
    else:
        instance.last_error = ""
    db.add(instance)
    db.commit()

    return {
        "ok": ok,
        "instance": instance.name,
        "stored": stored,
        "results": results,
    }

    return {"ok": ok, "instance": instance.name, "results": results}


# ============================================================ 备份与容灾


@router.get("/backups/templates", summary="备份脚本模板列表")
def backup_templates(_: CurrentUser) -> dict:
    from app.builtin_scripts.backup_templates import BACKUP_TEMPLATE_CHOICES

    return {"templates": BACKUP_TEMPLATE_CHOICES}


@router.get("/backups", response_model=list[BackupTaskOut], summary="备份任务列表")
def list_backups(
    _: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> list[BackupTaskOut]:
    names = {s.id: s.name for s in db.query(Server).all()}
    inst_names = {d.id: d.name for d in db.query(DatabaseInstance).all()}
    result = []
    for t in db.query(BackupTask).order_by(BackupTask.id).all():
        item = BackupTaskOut.model_validate(t)
        item.db_instance_name = inst_names.get(t.db_instance_id or -1, "")
        item.exec_server_name = names.get(t.exec_server_id or -1, "")
        result.append(item)
    return result


@router.post("/backups", response_model=BackupTaskOut, summary="新建备份任务")
def create_backup(
    payload: BackupTaskIn,
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
) -> BackupTaskOut:
    from app.services.backup import register_backup_task

    data = payload.model_dump()
    _validate_backup_refs(db, data)
    task = BackupTask(**data)
    db.add(task)
    db.commit()
    db.refresh(task)
    register_backup_task(task)
    return BackupTaskOut.model_validate(task)


@router.patch("/backups/{task_id}", response_model=BackupTaskOut, summary="修改备份任务")
def update_backup(
    task_id: int,
    payload: BackupTaskUpdate,
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
) -> BackupTaskOut:
    from app.services.backup import register_backup_task

    task = db.get(BackupTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    data = payload.model_dump(exclude_unset=True)
    _validate_backup_refs(db, data)
    for field, value in data.items():
        setattr(task, field, value)
    db.add(task)
    db.commit()
    db.refresh(task)
    register_backup_task(task)
    return BackupTaskOut.model_validate(task)


@router.delete("/backups/{task_id}", summary="删除备份任务")
def delete_backup(
    task_id: int, _: OperatorUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    from app.services.backup import unregister_backup_task

    task = db.get(BackupTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    unregister_backup_task(task_id)
    for run in db.query(BackupRun).filter(BackupRun.task_id == task_id).all():
        db.delete(run)
    db.delete(task)
    db.commit()
    return {"ok": True}


@router.post("/backups/{task_id}/run", summary="立即执行备份")
async def run_backup(task_id: int, _: OperatorUser) -> dict:
    from app.services.backup import run_backup_task

    return await run_backup_task(task_id, trigger="manual")


@router.get("/backups/{task_id}/runs", summary="备份执行历史")
def backup_runs(
    task_id: int,
    _: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=30, le=200),
) -> dict:
    runs = (
        db.query(BackupRun)
        .filter(BackupRun.task_id == task_id)
        .order_by(BackupRun.started_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "runs": [
            {
                "id": r.id,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "duration_ms": r.duration_ms,
                "exit_code": r.exit_code,
                "artifact": r.artifact,
                "artifact_size": r.artifact_size,
                "purged": r.purged,
                "trigger": r.trigger,
                "output": r.output,
                "error": r.error,
            }
            for r in runs
        ]
    }
