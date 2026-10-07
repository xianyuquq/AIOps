"""指标采集调度。

流程：
  APScheduler 触发 → 解析脚本作用域 → 并发在目标服务器执行
                   → 按 emit_schema 解析 → 批量入库 → 交给告警引擎评估

设计要点：
- 单次采集内的所有服务器并发执行，但整体并发受 collector_max_concurrency 限制。
- 单台失败不影响其他台（return_exceptions=True）。
- 连续失败的服务器会被标记为 offline，避免对已宕机机器反复重试拖慢采集。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.models import (
    DatabaseInstance,
    MetricDefinition,
    MetricSample,
    MetricScript,
    Server,
)
from app.services.script_runner import ParsedMetric, execute_script
from app.services.ssh_pool import SSHError, pool

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")

# 记录每个脚本最近一次执行时间，避免重复注册
_registered: set[int] = set()


def resolve_targets(db, scope: dict) -> list[Server]:
    """把 target_scope 解析为服务器列表。

    scope 形式：
      {"all": true}
      {"groups": [1, 2], "servers": [3, 4]}
      {}  -> 等价于 all
    """
    if not scope or scope.get("all"):
        return db.query(Server).all()

    group_ids = scope.get("groups") or []
    server_ids = scope.get("servers") or []

    query = db.query(Server)
    if not group_ids and not server_ids:
        return query.all()

    from sqlalchemy import or_

    conditions = []
    if group_ids:
        conditions.append(Server.group_id.in_(group_ids))
    if server_ids:
        conditions.append(Server.id.in_(server_ids))
    return query.filter(or_(*conditions)).all()


async def collect_script(script_id: int) -> dict:
    """执行一次采集。返回统计信息。

    runtime == "sql" 的脚本走数据库采集路径（真实驱动连接 / SSH 跳板），
    其余走主机采集路径（在目标服务器上执行 shell / 本地 python 沙箱）。
    """
    db = SessionLocal()
    try:
        script = db.get(MetricScript, script_id)
        if script is None or not script.enabled:
            return {"skipped": True, "script_id": script_id}

        if script.runtime == "sql":
            return await _collect_sql_script(db, script)

        targets = [
            s for s in resolve_targets(db, script.target_scope or {})
            if script.platform in ("any", s.platform)
        ]
        if not targets:
            return {"script_id": script_id, "servers": 0, "samples": 0}

        semaphore = asyncio.Semaphore(settings.collector_max_concurrency)
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        async def _one(server: Server):
            async with semaphore:
                try:
                    executor = await pool.get(server)
                    result = await execute_script(script, executor=executor)
                except SSHError as exc:
                    return server, None, str(exc)
                except Exception as exc:
                    logger.exception("采集 %s 失败", server.name)
                    return server, None, str(exc)

                if not result.ok:
                    return server, None, result.error or "采集失败"
                return server, result.metrics, None

        outcomes = await asyncio.gather(
            *[_one(s) for s in targets], return_exceptions=True
        )

        samples: list[MetricSample] = []
        failures = 0
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                failures += 1
                continue
            server, metrics, error = outcome
            if error is not None:
                failures += 1
                server.status = "offline"
                server.last_error = error[:500]
                continue
            server.status = "online"
            server.last_seen = now
            server.last_error = ""
            for m in metrics or []:
                samples.append(
                    MetricSample(
                        server_id=server.id,
                        metric_key=m.key,
                        ts=now,
                        value=m.value,
                        labels=m.labels or None,
                    )
                )

        if samples:
            db.bulk_save_objects(samples)
        db.commit()

        # 采集完成后评估告警
        if samples:
            try:
                from app.services.alert_engine import evaluate_samples

                await evaluate_samples(samples)
            except Exception:
                logger.exception("告警评估失败")

        return {
            "script_id": script_id,
            "servers": len(targets),
            "samples": len(samples),
            "failures": failures,
        }
    finally:
        db.close()


# --------------------------------------------------------------------------- 数据库采集


def resolve_db_targets(db, script: MetricScript):
    """解析 SQL 脚本要采集哪些数据库实例。

    规则：
      - 只取 enabled 的实例
      - db_type 需与脚本声明的 db_type 一致
      - 若 target_scope 指定了 database_instances，则进一步收窄
    """
    query = db.query(DatabaseInstance).filter(DatabaseInstance.enabled.is_(True))
    if script.db_type:
        query = query.filter(DatabaseInstance.db_type == script.db_type)

    scope = script.target_scope or {}
    ids = scope.get("database_instances") or []
    if ids:
        query = query.filter(DatabaseInstance.id.in_(ids))
    return query.all()


async def _collect_sql_script(db, script: MetricScript) -> dict:
    """采集数据库指标。"""
    from app.core.crypto import decrypt_secret
    from app.services import db_collector

    targets = resolve_db_targets(db, script)
    if not targets:
        return {
            "script_id": script.id,
            "databases": 0,
            "samples": 0,
            "note": "没有匹配的已启用数据库实例",
        }

    timeout = script.timeout_sec or 15
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    samples: list[MetricSample] = []
    failures = 0
    details: list[dict] = []

    for instance in targets:
        password = None
        if instance.secret_ciphertext and instance.secret_nonce:
            try:
                password = decrypt_secret(instance.secret_ciphertext, instance.secret_nonce)
            except Exception as exc:
                failures += 1
                instance.status = "offline"
                instance.last_error = f"凭据解密失败：{exc}"
                details.append({"instance": instance.name, "error": instance.last_error})
                continue

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
                failures += 1
                instance.status = "offline"
                instance.last_error = "采集方式为 SSH 跳板，但未指定跳板服务器"
                details.append({"instance": instance.name, "error": instance.last_error})
                continue
            proxy = db.get(Server, instance.proxy_server_id)
            if proxy is None:
                failures += 1
                instance.status = "offline"
                instance.last_error = "指定的跳板服务器不存在"
                details.append({"instance": instance.name, "error": instance.last_error})
                continue
            try:
                executor = await pool.get(proxy)
            except Exception as exc:
                failures += 1
                instance.status = "offline"
                instance.last_error = f"跳板服务器连接失败：{exc}"
                details.append({"instance": instance.name, "error": instance.last_error})
                continue

        try:
            if executor is not None:
                metrics = await db_collector.collect_via_proxy(
                    executor, info, script.body, timeout
                )
            else:
                metrics = await db_collector.collect_direct(info, script.body, timeout)
        except Exception as exc:
            failures += 1
            instance.status = "offline"
            instance.last_error = str(exc)[:500]
            details.append({"instance": instance.name, "error": instance.last_error})
            continue

        instance.status = "online"
        instance.last_error = ""
        for m in metrics:
            samples.append(
                MetricSample(
                    server_id=None,
                    db_instance_id=instance.id,
                    metric_key=m.key,
                    ts=now,
                    value=m.value,
                    labels=m.labels or None,
                )
            )
        details.append(
            {"instance": instance.name, "ok": True, "metrics": len(metrics)}
        )

    if samples:
        db.bulk_save_objects(samples)
    db.commit()

    if samples:
        try:
            from app.services.alert_engine import evaluate_samples

            await evaluate_samples(samples)
        except Exception:
            logger.exception("数据库指标告警评估失败")

    return {
        "script_id": script.id,
        "databases": len(targets),
        "samples": len(samples),
        "failures": failures,
        "details": details,
    }


def register_script(script: MetricScript) -> None:
    """为脚本注册定时任务。"""
    job_id = f"metric_script_{script.id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if not script.enabled:
        return
    scheduler.add_job(
        collect_script,
        "interval",
        seconds=max(script.interval_sec, 10),
        args=[script.id],
        id=job_id,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _registered.add(script.id)


def unregister_script(script_id: int) -> None:
    job_id = f"metric_script_{script_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    _registered.discard(script_id)


def reload_all_scripts() -> int:
    """重建所有采集任务。用于启动或脚本变更后。"""
    for job in scheduler.get_jobs():
        if job.id.startswith("metric_script_"):
            scheduler.remove_job(job.id)
    _registered.clear()

    db = SessionLocal()
    try:
        scripts = db.query(MetricScript).filter(MetricScript.enabled.is_(True)).all()
        for script in scripts:
            register_script(script)
        return len(scripts)
    finally:
        db.close()


async def purge_old_samples() -> int:
    """清理超过保留期的采样点。"""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=settings.metrics_retention_days
    )
    db = SessionLocal()
    try:
        result = db.execute(delete(MetricSample).where(MetricSample.ts < cutoff))
        db.commit()
        return result.rowcount or 0
    finally:
        db.close()


def sync_metric_definitions() -> int:
    """把内置脚本中声明的指标写入 metric_definition，用于前端展示单位等信息。"""
    db = SessionLocal()
    try:
        existing = {m.key for m in db.query(MetricDefinition).all()}
        added = 0
        for script in db.query(MetricScript).all():
            schema = script.emit_schema or {}
            for spec in schema.get("metrics", []):
                key = spec.get("key")
                if not key or key in existing:
                    continue
                db.add(
                    MetricDefinition(
                        key=key,
                        name=spec.get("name") or key,
                        unit=spec.get("unit", ""),
                        category=script.category,
                        value_type=spec.get("type", "gauge"),
                        description=spec.get("description", ""),
                    )
                )
                existing.add(key)
                added += 1
        db.commit()
        return added
    finally:
        db.close()


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.start()
        # 保留策略：每天凌晨 3 点清理
        scheduler.add_job(
            purge_old_samples,
            "cron",
            hour=3,
            minute=0,
            id="purge_old_samples",
            replace_existing=True,
        )


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
