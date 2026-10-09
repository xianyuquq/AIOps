"""FastAPI 应用入口。

启动流程：
  1. 建表
  2. 创建默认管理员
  3. 导入内置监控脚本与告警规则（仅首次）
  4. 启动采集调度器
  5. 注册前端静态资源
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import SessionLocal, init_db
from app.migrate import MIGRATION_STATE, run_migrations

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def seed_defaults() -> None:
    """首次启动时导入内置数据。"""
    from app.builtin_scripts import BUILTIN_ALERT_RULES, BUILTIN_SCRIPTS
    from app.core.crypto import hash_password
    from app.models import AlertRule, BackupTask, DatabaseInstance, MetricScript, User, utcnow

    db = SessionLocal()
    try:
        # 默认管理员
        if db.query(User).count() == 0:
            db.add(
                User(
                    username=settings.admin_username,
                    password_hash=hash_password(settings.admin_password),
                    role="admin",
                    display_name="系统管理员",
                )
            )
            db.commit()
            logger.info(
                "已创建默认管理员：%s / %s（请尽快修改密码）",
                settings.admin_username,
                settings.admin_password,
            )

        # 内置监控脚本：按名称同步
        # 关键点：内置脚本的**内容由代码拥有**（界面本就禁止修改其正文），
        # 因此每次启动都应把正文/解析规则同步为代码里的最新版本，
        # 否则升级后老库永远停留在旧脚本上——比如新增了一个指标，
        # 老库采集不到却看不出原因。
        # 用户可调的运行参数（启用状态、周期、超时、作用域）必须保留。
        by_name = {s["name"]: s for s in BUILTIN_SCRIPTS}
        existing = {
            s.name: s
            for s in db.query(MetricScript).filter(MetricScript.is_builtin.is_(True)).all()
        }
        added = updated = 0
        for name, spec in by_name.items():
            cur = existing.get(name)
            if cur is None:
                db.add(
                    MetricScript(
                        name=spec["name"],
                        category=spec["category"],
                        runtime=spec["runtime"],
                        platform=spec["platform"],
                        db_type=spec.get("db_type", ""),
                        body=spec["body"],
                        emit_schema=spec["emit_schema"],
                        interval_sec=spec.get("interval_sec", 60),
                        timeout_sec=spec.get("timeout_sec", 15),
                        target_scope={"all": True},
                        enabled=False,
                        is_builtin=True,
                    )
                )
                added += 1
                continue

            changed = False
            for field, value in (
                ("category", spec["category"]),
                ("runtime", spec["runtime"]),
                ("platform", spec["platform"]),
                ("db_type", spec.get("db_type", "")),
                ("body", spec["body"]),
                ("emit_schema", spec["emit_schema"]),
            ):
                if getattr(cur, field) != value:
                    setattr(cur, field, value)
                    changed = True
            # 补齐历史遗留的空 db_type
            if not cur.db_type and spec.get("db_type"):
                cur.db_type = spec["db_type"]
                changed = True
            if changed:
                cur.updated_at = utcnow()
                db.add(cur)
                updated += 1

        if added or updated:
            db.commit()
            logger.info(
                "内置监控脚本已同步：新增 %d 个，更新 %d 个（用户设置的启用状态与周期保持不变）",
                added,
                updated,
            )
        # 非内置的自定义脚本不动

        # 内置备份脚本模板
        if db.query(BackupTask).count() == 0 and db.query(DatabaseInstance).count() > 0:
            _seed_backup_tasks(db)

        # 内置告警规则
        if db.query(AlertRule).count() == 0:
            for spec in BUILTIN_ALERT_RULES:
                db.add(
                    AlertRule(
                        name=spec["name"],
                        metric_key=spec["metric_key"],
                        operator=spec["operator"],
                        threshold=spec["threshold"],
                        duration_sec=spec.get("duration_sec", 60),
                        severity=spec.get("severity", "warning"),
                        target_scope={"all": True},
                    )
                )
            db.commit()
            logger.info("已导入 %d 条内置告警规则", len(BUILTIN_ALERT_RULES))
    finally:
        db.close()


def _seed_backup_tasks(db) -> None:
    """为已有的数据库实例生成备份任务草稿（默认停用，用户确认后再开）。"""
    from app.builtin_scripts.backup_templates import BACKUP_TEMPLATES
    from app.models import BackupTask, DatabaseInstance

    created = 0
    instances = db.query(DatabaseInstance).all()
    for instance in instances:
        template = BACKUP_TEMPLATES.get(instance.db_type)
        if template is None:
            continue
        exists = (
            db.query(BackupTask)
            .filter(BackupTask.db_instance_id == instance.id)
            .first()
        )
        if exists:
            continue
        db.add(
            BackupTask(
                name=f"{instance.name} 逻辑备份",
                db_instance_id=instance.id,
                exec_server_id=instance.proxy_server_id,
                backup_type="logical",
                template_key=f"{instance.db_type}_logical",
                script=template["script"],
                target_dir=template.get("target_dir", "/var/backups/aiops"),
                schedule_cron=template.get("schedule_cron", "0 2 * * *"),
                retention_days=7,
                enabled=False,
            )
        )
        created += 1
    if created:
        db.commit()
        logger.info("已为 %d 个数据库实例生成备份任务草稿", created)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    # 先执行 schema 迁移（补列/重建表），再 create_all 建新表
    run_migrations()
    init_db()
    seed_defaults()

    from app.services import collector

    collector.sync_metric_definitions()
    collector.start_scheduler()
    count = collector.reload_all_scripts()
    logger.info("采集调度器已启动，共 %d 个任务", count)

    from app.services.backup import reload_backup_tasks

    backup_count = reload_backup_tasks()
    logger.info("备份调度已启动，共 %d 个任务", backup_count)

    if not settings.llm_enabled:
        logger.info(
            "AI 功能未启用（AIOPS_LLM_ENABLED=false）。"
            "监控、终端、告警功能不受影响；设置 AIOPS_LLM_API_KEY 后可开启智能分析。"
        )

    yield

    from app.services.ssh_pool import pool
    from app.services.terminal import hub

    await hub.close_all()
    await pool.close_all()
    collector.shutdown_scheduler()
    logger.info("已关闭所有连接与调度器")


app = FastAPI(
    title=settings.app_name,
    description=(
        "AI 智能运维平台：智能监控、批量终端同步广播、AI 告警根因分析。\n\n"
        "**首次使用**：默认账号 admin / admin123，请立即修改。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # 生产环境应收紧为具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------- 路由

from app.api import (  # noqa: E402
    alerts_ai,
    auth,
    integration,
    metrics,
    servers,
    settings_api,
)
from app.ws import terminal_ws  # noqa: E402

app.include_router(auth.router)
app.include_router(servers.router)
app.include_router(metrics.router)
app.include_router(alerts_ai.router)
app.include_router(settings_api.router, prefix="/api/v1")
app.include_router(integration.router, prefix="/api/v1")
app.include_router(terminal_ws.router)


@app.get("/api/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    from app.services.ai.provider import get_provider
    from app.services.ssh_pool import pool
    from app.services.terminal import hub

    return {
        "status": "ok",
        "app": settings.app_name,
        "ai_enabled": get_provider().enabled,
        "ssh_pool_active": pool.active_count,
        "terminal_sessions": hub.session_count,
        # 迁移失败时平台仍可用，但数据库指标写入会不可用，这里如实暴露
        "schema_ok": MIGRATION_STATE.get("ok", True),
        "schema_message": MIGRATION_STATE.get("message", ""),
    }


# --------------------------------------------------------------------------- 前端静态资源

# app/main.py -> app/ -> backend/ -> 仓库根目录
BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent

# 依次尝试几个可能的构建产物位置，便于前后端分目录部署
_CANDIDATES = [
    PROJECT_ROOT / "frontend" / "dist",   # 仓库根/frontend/dist（默认）
    BACKEND_DIR / "frontend" / "dist",    # backend/frontend/dist
    BACKEND_DIR / "static",               # 单独拷贝到后端的场景
]
FRONTEND_DIST = next((p for p in _CANDIDATES if (p / "index.html").is_file()), None)
if FRONTEND_DIST is None:
    FRONTEND_DIST = _CANDIDATES[0]

if FRONTEND_DIST and (FRONTEND_DIST / "index.html").is_file():
    logger.info("已挂载前端静态资源：%s", FRONTEND_DIST)
    app.mount(
        "/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets"
    )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(str(FRONTEND_DIST / "index.html"))

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        """SPA 路由回退：非 /api 路径一律返回 index.html。"""
        if full_path.startswith(("api/", "ws/")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = FRONTEND_DIST / full_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(FRONTEND_DIST / "index.html"))

else:
    logger.warning(
        "未找到前端构建产物（已尝试：%s）。仅提供 API；"
        "请在 frontend 目录执行 npm install && npm run build。",
        ", ".join(str(p) for p in _CANDIDATES),
    )

    @app.get("/", include_in_schema=False)
    def index_placeholder() -> dict:
        return {
            "message": f"{settings.app_name} 后端已启动",
            "hint": "前端尚未构建。请在 frontend 目录执行 npm install && npm run build",
            "docs": "/docs",
        }
