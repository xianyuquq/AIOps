"""指标查询、监控脚本管理、脚本试运行。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, OperatorUser
from app.db import get_db
from app.models import (
    DatabaseInstance,
    MetricDefinition,
    MetricSample,
    MetricScript,
    Server,
)
from app.schemas import (
    DashboardSummary,
    MetricDefinitionOut,
    MetricScriptIn,
    MetricScriptOut,
    MetricScriptUpdate,
    MetricSeries,
    MetricPoint,
    ScriptTestRequest,
    ScriptTestResult,
)
from app.services import collector
from app.services.alert_engine import compute_summary
from app.services.script_runner import assess_script_risk, execute_script
from app.services.ssh_pool import pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["监控"])

# 内置脚本：不可修改的「采集内容」字段
_BUILTIN_LOCKED_FIELDS = (
    "name", "category", "runtime", "platform", "body", "emit_schema",
)
# 内置脚本：允许调整的「运行参数」字段
_BUILTIN_ALLOWED_FIELDS = ("enabled", "target_scope", "interval_sec", "timeout_sec")


# --------------------------------------------------------------------------- 指标定义


@router.get("/metrics/definitions", response_model=list[MetricDefinitionOut],
            summary="指标定义列表")
def list_definitions(
    _: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> list[MetricDefinitionOut]:
    return [
        MetricDefinitionOut.model_validate(m)
        for m in db.query(MetricDefinition).order_by(MetricDefinition.key).all()
    ]


@router.get("/metrics/available", summary="已采集的指标名列表")
def available_metrics(
    _: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    rows = db.query(MetricSample.metric_key).distinct().all()
    return {"metrics": sorted(r[0] for r in rows)}


def _target_names(db) -> dict:
    """构造「采样点归属名」查表。

    主机指标的 key 是 (server_id, None)，数据库指标的 key 是 (None, db_instance_id)。
    """
    from app.models import DatabaseInstance

    names: dict[tuple, str] = {}
    for s in db.query(Server).all():
        names[(s.id, None)] = s.name
    for d in db.query(DatabaseInstance).all():
        names[(None, d.id)] = d.name
    return names


@router.get("/metrics/series", response_model=list[MetricSeries], summary="查询指标时序")
def query_series(
    _: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    metric_key: str = Query(...),
    server_ids: str | None = Query(default=None, description="逗号分隔的服务器 ID"),
    hours: int = Query(default=6, ge=1, le=720),
    limit: int = Query(default=500, ge=10, le=5000),
) -> list[MetricSeries]:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)

    query = db.query(MetricSample).filter(
        MetricSample.metric_key == metric_key,
        MetricSample.ts >= since,
    )
    if server_ids:
        try:
            ids = [int(x) for x in server_ids.split(",") if x.strip()]
            query = query.filter(MetricSample.server_id.in_(ids))
        except ValueError:
            raise HTTPException(status_code=400, detail="server_ids 格式错误") from None

    samples = query.order_by(MetricSample.ts.asc()).limit(limit * 20).all()

    names = _target_names(db)
    definition = (
        db.query(MetricDefinition).filter(MetricDefinition.key == metric_key).first()
    )
    unit = definition.unit if definition else ""

    # 按归属对象分组：主机按 server_id，数据库按 db_instance_id
    grouped: dict[tuple, list[MetricPoint]] = {}
    for sample in samples:
        owner = (sample.server_id, sample.db_instance_id)
        grouped.setdefault(owner, []).append(
            MetricPoint(ts=sample.ts, value=sample.value, labels=sample.labels)
        )

    result: list[MetricSeries] = []
    for (server_id, db_instance_id), points in grouped.items():
        owner_id = db_instance_id if db_instance_id is not None else server_id
        result.append(
            MetricSeries(
                server_id=owner_id or 0,
                server_name=names.get((server_id, db_instance_id), f"#{owner_id}"),
                metric_key=metric_key,
                unit=unit,
                points=sorted(points, key=lambda p: p.ts)[-limit:],
            )
        )
    return result


@router.get("/metrics/latest", summary="各服务器/数据库某指标的最新值")
def latest_metric(
    _: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    metric_key: str = Query(...),
) -> dict:
    rows = _latest_samples(db, metric_key)
    names = _target_names(db)
    return {
        "metric_key": metric_key,
        "results": [
            {
                "server_id": sample.server_id or sample.db_instance_id,
                "server_name": names.get(
                    (sample.server_id, sample.db_instance_id), "未知"
                ),
                "target_type": "database" if sample.db_instance_id else "server",
                "value": round(sample.value, 2),
                "labels": sample.labels or {},
                "ts": sample.ts.isoformat(),
            }
            for sample in rows
        ],
    }


def _latest_samples(db, metric_key: str) -> list[MetricSample]:
    """取每个归属对象（服务器或数据库实例）该指标的最新采样点。"""
    rows = (
        db.query(MetricSample)
        .filter(MetricSample.metric_key == metric_key)
        .order_by(MetricSample.ts.desc())
        .limit(4000)
        .all()
    )
    seen: set[tuple] = set()
    latest: list[MetricSample] = []
    for sample in rows:
        owner = (sample.server_id, sample.db_instance_id)
        if owner in seen:
            continue
        seen.add(owner)
        latest.append(sample)
    latest.sort(key=lambda s: s.value, reverse=True)
    return latest


@router.get("/metrics/forecast", summary="指标趋势预测（含历史与外推）")
def metric_forecast(
    _: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    metric_key: str = Query(...),
    target_type: str = Query(default="server", description="server | database"),
    target_id: int = Query(...),
    hours: int = Query(default=24, ge=2, le=720),
    horizon_hours: int = Query(default=24, ge=1, le=720),
) -> dict:
    """对单个对象（服务器或数据库实例）的某指标做预测。

    返回历史点与外推点，供前端在图表上叠加预测线。
    """
    from app.services.forecast import FORECAST_METRICS, build_forecast

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)

    query = db.query(MetricSample).filter(
        MetricSample.metric_key == metric_key,
        MetricSample.ts >= since,
    )
    if target_type == "database":
        query = query.filter(MetricSample.db_instance_id == target_id)
        target = db.get(DatabaseInstance, target_id)
        target_name = target.name if target else f"#{target_id}"
    else:
        query = query.filter(MetricSample.server_id == target_id)
        target = db.get(Server, target_id)
        target_name = target.name if target else f"#{target_id}"

    samples = query.order_by(MetricSample.ts.asc()).limit(5000).all()
    points = [(s.ts, s.value) for s in samples]
    if not points:
        raise HTTPException(status_code=404, detail="该时间段内没有采样数据")

    label, unit, hard_limit, mode = FORECAST_METRICS.get(
        metric_key, (metric_key, "", None, "growth")
    )
    if mode == "peak":
        overview = compute_database_overview(db)
        match = next((x for x in overview if x["id"] == target_id), None)
        hard_limit = (match or {}).get("connections_max")

    fc = build_forecast(
        points,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        metric_key=metric_key,
        label=label,
        unit=unit,
        limit=hard_limit,
        horizon_hours=horizon_hours,
        mode=mode,
    )
    if fc is None:
        raise HTTPException(status_code=400, detail="样本不足，无法预测")

    payload = fc.to_dict()
    payload["forecastable"] = metric_key in FORECAST_METRICS
    return payload


@router.get("/dashboard/summary", response_model=DashboardSummary, summary="大盘汇总")
def dashboard_summary(
    _: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> DashboardSummary:
    return DashboardSummary(**compute_summary(db))


# --------------------------------------------------------------------------- 监控脚本


def _script_out(script: MetricScript) -> MetricScriptOut:
    return MetricScriptOut.model_validate(script)


@router.get("/scripts", response_model=list[MetricScriptOut], summary="脚本列表")
def list_scripts(
    _: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> list[MetricScriptOut]:
    return [
        _script_out(s)
        for s in db.query(MetricScript).order_by(MetricScript.category, MetricScript.name).all()
    ]


@router.post("/scripts", response_model=MetricScriptOut, summary="新建脚本")
def create_script(
    payload: MetricScriptIn,
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
) -> MetricScriptOut:
    script = MetricScript(**payload.model_dump())
    db.add(script)
    db.commit()
    db.refresh(script)
    collector.register_script(script)
    collector.sync_metric_definitions()
    return _script_out(script)


@router.get("/scripts/{script_id}", response_model=MetricScriptOut, summary="脚本详情")
def get_script(
    script_id: int, _: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> MetricScriptOut:
    script = db.get(MetricScript, script_id)
    if script is None:
        raise HTTPException(status_code=404, detail="脚本不存在")
    return _script_out(script)


@router.patch("/scripts/{script_id}", response_model=MetricScriptOut,
              summary="修改脚本")
def update_script(
    script_id: int,
    payload: MetricScriptUpdate,
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
) -> MetricScriptOut:
    """修改脚本。

    内置脚本的规则：
    - **允许**调整运行参数（启用/停用、采集周期、超时、作用域）
      —— 这是必须的，内置脚本默认停用，用户要能开启它们。
    - **不允许**修改采集内容（脚本正文、输出解析、运行时、平台、名称、分类）
      —— 需要定制的请用「复制」生成副本后再改。
    """
    script = db.get(MetricScript, script_id)
    if script is None:
        raise HTTPException(status_code=404, detail="脚本不存在")

    data = payload.model_dump(exclude_unset=True)

    if script.is_builtin:
        # 只有当传入值**确实与原值不同**时才拒绝，
        # 这样客户端把全部字段原样回传（常见于切换开关）也不会被误拦。
        locked = [
            field
            for field in _BUILTIN_LOCKED_FIELDS
            if field in data and data[field] != getattr(script, field)
        ]
        if locked:
            raise HTTPException(
                status_code=400,
                detail=(
                    "内置脚本的采集内容不可修改（涉及字段："
                    + "、".join(locked)
                    + "）。如需定制，请点击「复制」生成副本后再编辑。"
                ),
            )
        data = {k: v for k, v in data.items() if k in _BUILTIN_ALLOWED_FIELDS}

    for field, value in data.items():
        setattr(script, field, value)
    db.add(script)
    db.commit()
    db.refresh(script)

    # 启用状态或周期变化后重建采集任务
    collector.register_script(script)
    collector.sync_metric_definitions()
    return _script_out(script)


@router.post("/scripts/{script_id}/duplicate", response_model=MetricScriptOut,
             summary="复制脚本")
def duplicate_script(
    script_id: int,
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
) -> MetricScriptOut:
    """复制一份可自由编辑的脚本副本（内置脚本定制入口）。"""
    src = db.get(MetricScript, script_id)
    if src is None:
        raise HTTPException(status_code=404, detail="脚本不存在")

    base_name = f"{src.name}（副本）"
    name = base_name
    index = 2
    while db.query(MetricScript).filter(MetricScript.name == name).first():
        name = f"{base_name}{index}"
        index += 1

    copy = MetricScript(
        name=name,
        category=src.category,
        runtime=src.runtime,
        platform=src.platform,
        body=src.body,
        emit_schema=src.emit_schema,
        interval_sec=src.interval_sec,
        timeout_sec=src.timeout_sec,
        target_scope=src.target_scope,
        enabled=False,      # 副本默认停用，避免立刻重复采集
        is_builtin=False,
    )
    db.add(copy)
    db.commit()
    db.refresh(copy)
    return _script_out(copy)


@router.delete("/scripts/{script_id}", summary="删除脚本")
def delete_script(
    script_id: int, _: OperatorUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    script = db.get(MetricScript, script_id)
    if script is None:
        raise HTTPException(status_code=404, detail="脚本不存在")
    if script.is_builtin:
        raise HTTPException(status_code=400, detail="内置脚本不可删除")
    collector.unregister_script(script_id)
    db.delete(script)
    db.commit()
    return {"ok": True}


@router.post("/scripts/{script_id}/run", response_model=ScriptTestResult,
             summary="试运行脚本")
async def run_script(
    script_id: int,
    payload: ScriptTestRequest,
    _: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
) -> ScriptTestResult:
    """在选中服务器上试跑脚本，返回原始输出与解析后的指标。

    这是脚本上线前的重要防线：解析格式错误在这里就能发现。
    """
    script = db.get(MetricScript, script_id)
    if script is None:
        raise HTTPException(status_code=404, detail="脚本不存在")

    executor = None
    if script.runtime != "python":
        if payload.server_id is None:
            # 未指定则用作用域内第一台
            targets = collector.resolve_targets(db, script.target_scope or {})
            if not targets:
                raise HTTPException(
                    status_code=400, detail="没有可用目标服务器，请先在作用域内添加服务器"
                )
            server = targets[0]
        else:
            server = db.get(Server, payload.server_id)
            if server is None:
                raise HTTPException(status_code=404, detail="服务器不存在")

        try:
            executor = await pool.get(server)
        except Exception as exc:
            return ScriptTestResult(ok=False, error=f"连接失败：{exc}")

    result = await execute_script(script, executor=executor)

    return ScriptTestResult(
        ok=result.ok,
        raw_output=result.raw_output,
        metrics=[
            {"key": m.key, "value": m.value, "labels": m.labels} for m in result.metrics
        ],
        error=result.error,
        duration_ms=result.duration_ms,
        truncated=result.truncated,
        risk_warning=result.risk_warning or assess_script_risk(script.body, script.runtime),
    )


@router.post("/scripts/validate", summary="校验脚本输出解析")
def validate_script(payload: MetricScriptIn, _: OperatorUser) -> dict:
    """用示例输出校验 emit_schema 的解析是否正确（不连接服务器）。"""
    from app.services.script_runner import parse_output

    sample = payload.emit_schema.get("sample_output", "")
    if not sample:
        return {"ok": True, "metrics": [], "message": "未提供样例输出，跳过校验"}
    try:
        metrics = parse_output(sample, payload.emit_schema)
    except Exception as exc:
        return {"ok": False, "metrics": [], "message": f"解析失败：{exc}"}
    return {
        "ok": bool(metrics),
        "metrics": [{"key": m.key, "value": m.value, "labels": m.labels} for m in metrics],
        "message": f"解析出 {len(metrics)} 个指标" if metrics else "未解析出任何指标，请检查格式",
    }


@router.post("/collector/reload", summary="重载采集任务")
def reload_collector(_: OperatorUser) -> dict:
    count = collector.reload_all_scripts()
    added = collector.sync_metric_definitions()
    return {"scripts_registered": count, "metric_definitions_added": added}


@router.post("/collector/run/{script_id}", summary="立即执行一次采集")
async def run_collect_now(script_id: int, _: OperatorUser) -> dict:
    return await collector.collect_script(script_id)
