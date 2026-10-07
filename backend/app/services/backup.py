"""容灾备份任务执行与调度。

执行模型：备份命令通过 SSH 在「执行服务器」上运行（通常就是数据库所在主机，
因为 mysqldump / pg_dump 需要在能访问数据库并装有客户端工具的机器上跑）。

安全设计：
- 数据库密码**不落进脚本正文**，运行时以环境变量注入
  （MYSQL_PWD / PGPASSWORD / DB_PASSWORD），避免出现在 ps 输出与日志里。
- 备份输出中的密码不会被回显（命令本身不含密码）。
- 记录每次运行的退出码、耗时、识别出的备份文件与大小，以及清理的文件数。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from apscheduler.triggers.cron import CronTrigger

from app.db import SessionLocal
from app.models import BackupRun, BackupTask, DatabaseInstance, Server
from app.services.ssh_pool import pool

logger = logging.getLogger(__name__)

JOB_PREFIX = "backup_task_"

# 命令白名单校验：备份命令允许的字符集足够宽，但拒绝明显危险的重定向到设备
_FORBIDDEN = re.compile(r">\s*/dev/[sh]d|mkfs\.|dd\s+if=.*of=/dev/")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def render_script(task: BackupTask, instance: DatabaseInstance | None) -> str:
    """把模板占位符替换为实际值。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    values = {
        "{{name}}": re.sub(r"[^\w\-]", "_", task.name or "backup"),
        "{{dir}}": task.target_dir or "/var/backups/aiops",
        "{{stamp}}": stamp,
        "{{retention}}": str(task.retention_days or 0),
        "{{host}}": instance.host if instance else "",
        "{{port}}": str(instance.port) if instance else "",
        "{{user}}": instance.username if instance else "",
        "{{database}}": (instance.database_name if instance else "") or "",
    }
    script = task.script
    for key, value in values.items():
        script = script.replace(key, value)
    # 统一换行符：脚本会在 Linux 上执行，若带 Windows 的 \r\n，
    # shell 会把 \r 当成参数的一部分，报出 `set: -` 这类莫名其妙的错误。
    # 用户在 Windows 上编辑/粘贴脚本是很常见的情况，这里必须兜住。
    script = script.replace("\r\n", "\n").replace("\r", "\n")
    return script


def _password_env(instance: DatabaseInstance | None) -> dict[str, str]:
    """构造密码环境变量（密码不进入命令行）。

    ⚠️ 金仓特殊：ksql / sys_dump **都不认 PGPASSWORD**，
    会转成交互式口令提示。因此金仓走 DB_PASSWORD，
    并在命令中用 `printf ... | sys_dump -W` 从 stdin 喂入口令。
    （这一点是在真实 KingbaseES V009R001C010 上实测得到的：
     用 PGPASSWORD 会让备份一直挂起，直到超时。）
    """
    if instance is None or not (instance.secret_ciphertext and instance.secret_nonce):
        return {}
    from app.core.crypto import decrypt_secret

    try:
        password = decrypt_secret(instance.secret_ciphertext, instance.secret_nonce)
    except Exception as exc:
        raise RuntimeError(f"凭据解密失败：{exc}") from exc

    if instance.db_type == "mysql":
        return {"MYSQL_PWD": password}
    if instance.db_type == "postgresql":
        return {"PGPASSWORD": password}
    # kingbase / oracle
    return {"DB_PASSWORD": password}


def _extract_artifact(stdout: str) -> str:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("BACKUP_FILE="):
            return line.split("=", 1)[1].strip()
    return ""


def _count_purged(stdout: str) -> int:
    """统计 find ... -delete 清理掉的文件数（-print -delete 会先打印路径）。"""
    count = 0
    for line in stdout.splitlines():
        line = line.strip()
        if line and "/" in line and not line.startswith("BACKUP_FILE="):
            if re.search(r"\.(sql\.gz|dump|dmp|tar\.gz|gz|log)$", line):
                count += 1
    return count


async def run_backup_task(task_id: int, trigger: str = "manual") -> dict:
    """执行一次备份任务，并记录历史。"""
    db = SessionLocal()
    try:
        task = db.get(BackupTask, task_id)
        if task is None:
            return {"ok": False, "error": "任务不存在"}

        instance = db.get(DatabaseInstance, task.db_instance_id) if task.db_instance_id else None

        # 校验命令
        if _FORBIDDEN.search(task.script):
            return {"ok": False, "error": "备份命令包含高危重定向，已拒绝执行"}

        # 执行服务器
        server = None
        if task.exec_server_id:
            server = db.get(Server, task.exec_server_id)
        if server is None and instance is not None and instance.proxy_server_id:
            server = db.get(Server, instance.proxy_server_id)
        if server is None:
            return {
                "ok": False,
                "error": "未指定执行服务器。备份命令需要在能访问数据库并装有客户端工具的"
                "机器上执行，请在任务中选择「执行服务器」。",
            }

        command = render_script(task, instance)
        timeout = task.timeout_sec or 1800

        run = BackupRun(
            task_id=task.id,
            status="running",
            started_at=_now(),
            trigger=trigger,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id

        server_ref = server
        task_snapshot = {
            "name": task.name,
            "db_type": instance.db_type if instance else "",
        }
        password_env: dict[str, str] = {}
        try:
            password_env = _password_env(instance)
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
            run.finished_at = _now()
            db.add(run)
            db.commit()
            return {"ok": False, "error": str(exc), "run_id": run_id}
    finally:
        db.close()

    # ---------- 在事件循环外执行（SSH 调用是异步的，这里保持异步）----------
    started = datetime.now(timezone.utc)
    try:
        executor = await pool.get(server_ref)
        # 必须用 export：`VAR=x cmd` 只对**那一条**命令生效，
        # 而这里后面跟着的是多行脚本（set -e / mkdir / sys_dump …），
        # 用前缀赋值会导致变量在脚本里为空——金仓就会报
        # 「fe_sendauth: no password supplied」。
        # export 还能让子进程（如 docker exec）继承到该变量。
        env_prefix = " ".join(f"export {k}={_shq(v)};" for k, v in password_env.items())
        full = f"{env_prefix} {command}" if env_prefix else command

        result = await executor.run(full, timeout=timeout)
        exit_code = result.exit_code
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        ok = result.ok
    except Exception as exc:
        exit_code = None
        stdout = ""
        stderr = f"{type(exc).__name__}: {exc}"
        ok = False

    duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)

    # ---------- 记录结果 ----------
    db = SessionLocal()
    try:
        run = db.get(BackupRun, run_id)
        task = db.get(BackupTask, task_id)
        if run is not None:
            run.status = "success" if ok else "failed"
            run.finished_at = _now()
            run.duration_ms = duration_ms
            run.exit_code = exit_code
            run.artifact = _extract_artifact(stdout)
            run.purged = _count_purged(stdout)
            run.output = (stdout or "")[:8000]
            run.error = (stderr or "")[:4000]
            db.add(run)
        if task is not None:
            task.last_run_at = _now()
            task.last_status = "success" if ok else "failed"
            task.last_duration_ms = duration_ms
            task.last_message = (
                f"备份文件：{_extract_artifact(stdout)}"
                if ok
                else (stderr or stdout or "执行失败")[:500]
            )
            db.add(task)
        db.commit()
    finally:
        db.close()

    logger.info(
        "备份任务 %s 执行%s（%dms）", task_snapshot["name"], "成功" if ok else "失败", duration_ms
    )
    return {
        "ok": ok,
        "run_id": run_id,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "artifact": _extract_artifact(stdout),
        "output": (stdout or "")[-2000:],
        "error": (stderr or "")[:1000],
    }


def _shq(value: str) -> str:
    """shell 单引号转义。"""
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


# --------------------------------------------------------------------------- 调度


def register_backup_task(task: BackupTask) -> bool:
    """按 cron 表达式注册备份任务。返回是否注册成功。"""
    from app.services.collector import scheduler

    job_id = f"{JOB_PREFIX}{task.id}"
    existing = scheduler.get_job(job_id)
    if existing:
        scheduler.remove_job(job_id)

    if not task.enabled or not task.schedule_cron:
        return False

    try:
        trigger = CronTrigger.from_crontab(task.schedule_cron)
    except ValueError as exc:
        logger.warning("备份任务 %s 的 cron 表达式无效：%s", task.name, exc)
        return False

    scheduler.add_job(
        run_backup_task,
        trigger,
        args=[task.id, "schedule"],
        id=job_id,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return True


def unregister_backup_task(task_id: int) -> None:
    from app.services.collector import scheduler

    job_id = f"{JOB_PREFIX}{task_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def reload_backup_tasks() -> int:
    """重建所有备份调度。"""
    from app.services.collector import scheduler

    for job in scheduler.get_jobs():
        if job.id.startswith(JOB_PREFIX):
            scheduler.remove_job(job.id)

    db = SessionLocal()
    try:
        count = 0
        for task in db.query(BackupTask).filter(BackupTask.enabled.is_(True)).all():
            if register_backup_task(task):
                count += 1
        return count
    finally:
        db.close()
