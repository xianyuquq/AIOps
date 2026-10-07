"""告警引擎：规则评估 + 降噪聚合。

降噪设计（见 docs/ARCHITECTURE.md §5.4）：
1. **持续时间**：指标需在 duration_sec 内持续满足条件才触发，消除毛刺。
2. **指纹聚合**：指纹 = hash(rule_id + metric_key + labels)。
   同一故障在 50 台机器同时触发时合并为 1 条"批量告警"，而非 50 条。
3. **静默窗口**：已确认的告警在 silence_min 内不再重复推送。
4. **自动恢复**：指标回到阈值内时自动 resolve 对应告警。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import AlertEvent, AlertRule, DatabaseInstance, MetricSample, Server

logger = logging.getLogger(__name__)

OPERATORS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_fingerprint(rule_id: int, metric_key: str, labels: dict | None) -> str:
    """生成告警指纹，用于聚合同一类故障。"""
    label_part = ""
    if labels:
        label_part = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    raw = f"{rule_id}|{metric_key}|{label_part}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _scope_matches(scope: dict, server: Server) -> bool:
    if not scope or scope.get("all"):
        return True
    groups = scope.get("groups") or []
    servers = scope.get("servers") or []
    if not groups and not servers:
        return True
    if server.id in servers:
        return True
    if server.group_id is not None and server.group_id in groups:
        return True
    return False


def _condition_met(operator: str, value: float, threshold: float) -> bool:
    fn = OPERATORS.get(operator)
    if fn is None:
        return False
    return fn(value, threshold)


async def evaluate_samples(samples: list[MetricSample]) -> int:
    """对一批采样点做规则评估。返回新产生的告警数量。

    注意：samples 是已从会话中 detach 的对象，这里只读取其属性值。
    """
    if not samples:
        return 0

    db = SessionLocal()
    created = 0
    try:
        rules = db.query(AlertRule).filter(AlertRule.enabled.is_(True)).all()
        if not rules:
            return 0

        # 按 metric_key 建索引，避免对每个样本都遍历全部规则
        rules_by_metric: dict[str, list[AlertRule]] = {}
        for rule in rules:
            rules_by_metric.setdefault(rule.metric_key, []).append(rule)

        now = _now()

        for sample in samples:
            matched_rules = rules_by_metric.get(sample.metric_key)
            if not matched_rules:
                continue

            server = db.get(Server, sample.server_id)
            if server is None:
                continue

            for rule in matched_rules:
                if not _scope_matches(rule.target_scope or {}, server):
                    continue
                if not _condition_met(rule.operator, sample.value, rule.threshold):
                    continue

                # 要求持续满足：检查 duration_sec 窗口内的最早采样点
                window_start = now - timedelta(seconds=max(rule.duration_sec, 0))
                if rule.duration_sec > 0:
                    oldest = db.execute(
                        select(MetricSample.ts)
                        .where(
                            MetricSample.server_id == sample.server_id,
                            MetricSample.metric_key == sample.metric_key,
                            MetricSample.ts >= window_start,
                        )
                        .order_by(MetricSample.ts.asc())
                        .limit(1)
                    ).scalar()
                    if oldest is None or oldest > window_start:
                        # 窗口内数据不足，说明刚开始超标，继续观察
                        continue

                created += _fire_or_update(db, rule, server, sample, now)

        db.commit()
        # 触发 AI 根因分析（异步，失败不影响告警落库）
        if created:
            _schedule_ai_analysis()
        return created
    finally:
        db.close()


def _fire_or_update(db: Session, rule: AlertRule, server: Server,
                    sample: MetricSample, now: datetime) -> int:
    """触发新告警，或更新已有告警的聚合计数。"""
    fingerprint = make_fingerprint(rule.id, sample.metric_key, sample.labels)

    existing = (
        db.query(AlertEvent)
        .filter(
            AlertEvent.fingerprint == fingerprint,
            AlertEvent.status == "firing",
        )
        .first()
    )

    label_text = ""
    if sample.labels:
        label_text = " ".join(f"{k}={v}" for k, v in sample.labels.items())

    if existing is not None:
        # 同一指纹已在告警中：若是不同服务器则累加受影响数量（聚合降噪）
        if existing.server_id != server.id:
            existing.affected_count = (existing.affected_count or 1) + 1
            detail = existing.detail or {}
            affected = detail.setdefault("affected_servers", [])
            if server.name not in affected:
                affected.append(server.name)
            existing.detail = detail
            db.add(existing)
        return 0

    detail = {
        "metric_key": sample.metric_key,
        "value": sample.value,
        "threshold": rule.threshold,
        "operator": rule.operator,
        "labels": sample.labels or {},
        "duration_sec": rule.duration_sec,
        "affected_servers": [server.name],
    }

    title = (
        f"{server.name} {rule.name}：{sample.metric_key} = {sample.value:.2f}"
        f" {rule.operator} {rule.threshold}"
    )
    if label_text:
        title += f" ({label_text})"

    db.add(
        AlertEvent(
            rule_id=rule.id,
            server_id=server.id,
            severity=rule.severity,
            title=title,
            detail=detail,
            status="firing",
            fired_at=now,
            fingerprint=fingerprint,
            affected_count=1,
        )
    )
    logger.info("告警触发：%s", title)
    return 1


def _schedule_ai_analysis() -> None:
    """异步触发 AI 根因分析。"""
    import asyncio

    async def _run():
        try:
            from app.services.ai.analyzer import analyze_pending_alerts

            await analyze_pending_alerts()
        except Exception:
            logger.exception("AI 根因分析失败")

    try:
        asyncio.create_task(_run())
    except RuntimeError:
        pass


async def auto_resolve(db: Session | None = None) -> int:
    """自动恢复：指标已回到阈值内且持续一段时间的告警标记为 resolved。

    这里采取保守策略：仅当最近一次采样已不再满足条件，且距离触发已超过
    max(duration_sec, 300) 秒时，才认为恢复。
    """
    own_session = db is None
    db = db or SessionLocal()
    resolved = 0
    try:
        firing = db.query(AlertEvent).filter(AlertEvent.status == "firing").all()
        now = _now()
        for event in firing:
            if event.server_id is None:
                continue
            rule = db.get(AlertRule, event.rule_id) if event.rule_id else None

            latest = (
                db.query(MetricSample)
                .filter(
                    MetricSample.server_id == event.server_id,
                    MetricSample.metric_key == (event.detail or {}).get("metric_key", ""),
                )
                .order_by(MetricSample.ts.desc())
                .first()
            )
            if latest is None:
                continue

            overdue = (now - (event.fired_at or now)).total_seconds() > max(
                (rule.duration_sec if rule else 300), 300
            )
            if not overdue:
                continue

            still_bad = False
            if rule is not None:
                still_bad = _condition_met(rule.operator, latest.value, rule.threshold)
            if not still_bad:
                event.status = "resolved"
                event.resolved_at = now
                db.add(event)
                resolved += 1
        db.commit()
        return resolved
    finally:
        if own_session:
            db.close()


def acknowledge_alert(db: Session, alert_id: int, user_id: int, silence_min: int) -> AlertEvent | None:
    """确认告警并进入静默窗口。"""
    event = db.get(AlertEvent, alert_id)
    if event is None:
        return None
    event.status = "acknowledged"
    event.acknowledged_by = user_id
    detail = event.detail or {}
    detail["silenced_until"] = (
        _now() + timedelta(minutes=silence_min)
    ).isoformat()
    event.detail = detail
    db.add(event)
    db.commit()
    return event


def compute_database_overview(db: Session) -> list[dict]:
    """汇总每个数据库实例的关键指标最新值。

    为什么需要：数据库指标采集到了，但此前大盘只统计主机，
    用户在总览页完全看不到数据库情况——「采了却看不见」等于没采。

    除了原始指标，这里还归一化出几个跨数据库通用的字段
    （连接数、上限、命中率、复制延迟、锁等待），
    因为不同数据库的指标名不同（如 MySQL 是 threads_connected，
    PostgreSQL 是 connections.total），前端不该关心这些差异。
    """
    from app.models import DatabaseInstance

    instances = db.query(DatabaseInstance).order_by(DatabaseInstance.id).all()
    if not instances:
        return []

    # 取最近的数据库采样点，按 (实例, 指标) 去重保留最新
    rows = (
        db.query(MetricSample)
        .filter(MetricSample.db_instance_id.isnot(None))
        .order_by(MetricSample.ts.desc())
        .limit(4000)
        .all()
    )
    latest: dict[tuple[int, str], MetricSample] = {}
    for sample in rows:
        key = (sample.db_instance_id, sample.metric_key)
        if key not in latest:
            latest[key] = sample

    # 归一化字段的候选指标名（按优先级）
    def pick(instance_id: int, candidates: tuple[str, ...]):
        for name in candidates:
            sample = latest.get((instance_id, name))
            if sample is not None:
                return sample.value, name
        return None, None

    result: list[dict] = []
    for inst in instances:
        raw = {
            key: {"value": s.value, "labels": s.labels or {}, "ts": s.ts.isoformat()}
            for (iid, key), s in latest.items()
            if iid == inst.id
        }

        connections, conn_key = pick(
            inst.id,
            (
                "db.connections.total",
                "db.connections.threads_connected",
                "db.connections.active",
            ),
        )
        conn_max, _ = pick(inst.id, ("db.connections.max",))
        hit_rate, hit_key = pick(
            inst.id, ("db.blocks.hit_rate", "db.innodb.buffer_hit_rate")
        )
        lag, _ = pick(inst.id, ("db.replication.lag_sec",))
        locks, _ = pick(inst.id, ("db.locks.waiting",))
        slow, _ = pick(inst.id, ("db.slow_queries",))

        usage_pct = None
        if connections is not None and conn_max:
            usage_pct = round(connections / conn_max * 100, 1)

        updated = max(
            (s.ts for (iid, _), s in latest.items() if iid == inst.id), default=None
        )

        result.append(
            {
                "id": inst.id,
                "name": inst.name,
                "db_type": inst.db_type,
                "status": inst.status,
                "last_error": inst.last_error,
                "collect_mode": inst.collect_mode,
                "connection_label": conn_key,
                "connections": connections,
                "connections_max": conn_max,
                "connection_usage_pct": usage_pct,
                "hit_rate": hit_rate,
                "hit_rate_label": hit_key,
                "lag_sec": lag,
                "locks_waiting": locks,
                "slow_queries": slow,
                "metrics": raw,
                "metric_count": len(raw),
                "updated_at": updated.isoformat() if updated else None,
            }
        )
    return result


def compute_forecasts(db: Session, hours: int = 24, limit: int = 8) -> list[dict]:
    """对值得关注的指标做趋势预测，返回最需要关注的若干条。

    排序优先级：
      1. 有明确 ETA 的（还能撑多久）—— 越早触顶越靠前
      2. 其次按增长率排序
    只返回增长中的指标：下降趋势对运维一般不是风险。
    """
    from app.services.forecast import FORECAST_METRICS, build_forecast

    since = _now() - timedelta(hours=hours)

    # 按指标逐个查询，而不是一次性 LIMIT 全表。
    # 原因：主机指标一天可产生 3 万+ 采样点，一个全局 LIMIT 会把
    # **最新的**样本截掉（尤其像 db.size_mb 这种刚上线、样本少的指标），
    # 结果就是「明明有数据，却预测不出来」。
    # 每个指标取最近的若干点即可满足拟合需要。
    per_metric_limit = 3000
    grouped: dict[tuple, list[tuple]] = {}
    for metric_key in FORECAST_METRICS:
        samples = (
            db.query(MetricSample)
            .filter(
                MetricSample.ts >= since,
                MetricSample.metric_key == metric_key,
            )
            .order_by(MetricSample.ts.desc())
            .limit(per_metric_limit)
            .all()
        )
        for s in samples:
            owner = ("database", s.db_instance_id) if s.db_instance_id else ("server", s.server_id)
            grouped.setdefault((owner, metric_key), []).append((s.ts, s.value))

    if not grouped:
        return []

    # 每组内部按时间升序，供线性拟合使用
    for key in grouped:
        grouped[key].sort(key=lambda p: p[0])

    server_names = {s.id: s.name for s in db.query(Server).all()}
    db_names = {d.id: d.name for d in db.query(DatabaseInstance).all()}

    # 数据库连接数的上限需要从概览里取（各库指标名不同）
    conn_max: dict[int, float] = {}
    for item in compute_database_overview(db):
        if item["connections_max"]:
            conn_max[item["id"]] = item["connections_max"]

    results: list[dict] = []
    for (owner, metric_key), points in grouped.items():
        target_type, target_id = owner
        if target_id is None:
            continue
        label, unit, hard_limit, mode = FORECAST_METRICS[metric_key]
        target_name = (
            db_names.get(target_id) if target_type == "database" else server_names.get(target_id)
        ) or f"#{target_id}"

        # 连接数的上限按实例取
        if mode == "peak":
            hard_limit = conn_max.get(target_id)

        fc = build_forecast(
            points,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
            metric_key=metric_key,
            label=label,
            unit=unit,
            limit=hard_limit,
            horizon_hours=24.0,
            mode=mode,
        )
        if fc is None:
            continue
        d = fc.to_dict()
        d["history"] = []      # 大盘不需要明细，列表页再按需取
        d["projection"] = []
        results.append(d)

    # 排序：先按是否有 ETA，再按 ETA 升序，最后按日增速降序
    def sort_key(x: dict):
        eta = x.get("hours_to_limit")
        has_eta = eta is not None and eta > 0
        return (
            0 if has_eta else 1,
            eta if has_eta else float("inf"),
            -abs(x.get("slope_per_day") or 0),
        )

    risen = [r for r in results if r["trend"] == "rising"]
    return sorted(risen, key=sort_key)[:limit]


def compute_summary(db: Session) -> dict:
    """大盘汇总数据。"""
    from sqlalchemy import func

    from app.models import MetricScript

    server_total = db.query(func.count(Server.id)).scalar() or 0
    server_online = (
        db.query(func.count(Server.id)).filter(Server.status == "online").scalar() or 0
    )
    alert_firing = (
        db.query(func.count(AlertEvent.id))
        .filter(AlertEvent.status == "firing")
        .scalar()
        or 0
    )
    alert_critical = (
        db.query(func.count(AlertEvent.id))
        .filter(AlertEvent.status == "firing", AlertEvent.severity == "critical")
        .scalar()
        or 0
    )
    script_total = db.query(func.count(MetricScript.id)).scalar() or 0
    script_enabled = (
        db.query(func.count(MetricScript.id))
        .filter(MetricScript.enabled.is_(True))
        .scalar()
        or 0
    )

    database_overview = compute_database_overview(db)
    database_total = len(database_overview)
    database_online = sum(
        1 for d in database_overview if d["status"] == "online"
    )

    try:
        forecasts = compute_forecasts(db)
    except Exception:
        logger.exception("趋势预测计算失败")
        forecasts = []

    def _latest_metric(metric_key: str, limit: int = 5) -> list[dict]:
        """取每台服务器该指标的最新值，倒序返回 Top N。"""
        subq = (
            db.query(
                MetricSample.server_id,
                func.max(MetricSample.ts).label("max_ts"),
            )
            .filter(MetricSample.metric_key == metric_key)
            .group_by(MetricSample.server_id)
            .subquery()
        )
        rows = (
            db.query(MetricSample, Server.name)
            .join(subq, (MetricSample.server_id == subq.c.server_id)
                  & (MetricSample.ts == subq.c.max_ts))
            .join(Server, Server.id == MetricSample.server_id)
            .filter(MetricSample.metric_key == metric_key)
            .order_by(MetricSample.value.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "server_id": sample.server_id,
                "server_name": name,
                "value": round(sample.value, 2),
                "labels": sample.labels or {},
            }
            for sample, name in rows
        ]

    return {
        "server_total": server_total,
        "server_online": server_online,
        "alert_firing": alert_firing,
        "alert_critical": alert_critical,
        "script_total": script_total,
        "script_enabled": script_enabled,
        "top_cpu": _latest_metric("cpu.usage_percent"),
        "top_memory": _latest_metric("mem.used_percent"),
        "top_disk": _latest_metric("disk.used_percent"),
        # 数据库概况：此前大盘完全没有数据库信息，导致「采集了却看不见」
        "database_total": database_total,
        "database_online": database_online,
        "databases": database_overview,
        # 趋势预测：回答「照这个速度还有多久出事」
        "forecasts": forecasts,
    }
