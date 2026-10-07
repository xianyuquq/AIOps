"""MySQL 备份的真实端到端验证（走完整平台链路）。

与之前「在容器里跑同一条命令」的区别：
本机现在有 Git Bash（E:\\gitBash\\bin\\bash.exe）作为 POSIX shell，
于是**本机自己就能充当备份执行主机**，平台可以真的通过 SSH
下发内置 MySQL 备份模板并产出真实文件。

链路：平台 → SSH(本机 Git Bash) → 内置 mysql_logical 模板 → 容器内 mysqldump
      → 真实 .sql.gz → gzip 校验 + 内容抽样

用法：python tests/verify_mysql_backup_real.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testenv import LOCAL_DB_PASSWORD  # noqa: E402

BASE = "http://127.0.0.1:8848"
LOCAL_SSH_PORT = 2901
TARGET_DIR = "/e/Agent/.tmp/mysql-real"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


async def ensure_server(client, h) -> dict:
    items = (await client.get(f"{BASE}/api/v1/servers", headers=h)).json()
    for s in items:
        if s["host"] == "127.0.0.1" and s["port"] == LOCAL_SSH_PORT:
            return s
    return (await client.post(
        f"{BASE}/api/v1/servers",
        headers=h,
        json={
            "name": "本机(GitBash 真实执行)", "host": "127.0.0.1",
            "port": LOCAL_SSH_PORT, "username": "root",
            "protocol": "ssh", "auth_type": "password", "secret": "x", "tags": {},
        },
    )).json()


async def ensure_task(client, h, payload: dict, name: str) -> dict:
    items = (await client.get(f"{BASE}/api/v1/backups", headers=h)).json()
    for t in items:
        if t["name"] == name:
            await client.patch(f"{BASE}/api/v1/backups/{t['id']}", headers=h, json=payload)
            return t
    return (await client.post(f"{BASE}/api/v1/backups", headers=h, json=payload)).json()


async def main() -> int:
    from app.builtin_scripts.backup_templates import BACKUP_TEMPLATES

    async with httpx.AsyncClient(timeout=600, trust_env=False) as client:
        r = await client.post(
            f"{BASE}/api/v1/auth/login/json",
            json={"username": "admin", "password": "admin123"},
        )
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # ---------- 执行主机 ----------
        print("[1] 执行主机（本机 Git Bash）")
        srv = await ensure_server(client, h)
        check("执行服务器已就绪", bool(srv.get("id")), f"id={srv.get('id')}")
        t = (await client.post(f"{BASE}/api/v1/servers/{srv['id']}/test", headers=h)).json()
        check("SSH 可连接", t.get("ok") is True, str(t.get("message"))[:70])

        # 确认它真的能跑 POSIX 脚本
        from app.db import SessionLocal
        from app.models import Server
        from app.services.server_ref import to_ref
        from app.services.ssh_pool import pool

        db = SessionLocal()
        try:
            ref = to_ref(db.get(Server, srv["id"]))
        finally:
            db.close()
        ex = await pool.get(ref)
        try:
            r1 = await ex.run("echo $0; command -v mysqldump; gzip --version | head -1", timeout=60)
            out = (r1.stdout or "").strip()
            print("      " + out.replace("\n", "\n      "))
            check("★ 执行主机使用 POSIX shell", "bash" in out or "sh" in out, out.split("\n")[0])
            check("★ 能找到 mysqldump", "mysqldump" in out)
            check("有 gzip", "gzip" in out or "gzip" in (r1.stderr or ""))
        finally:
            await pool.close_all()

        mysql = next(
            (d for d in (await client.get(f"{BASE}/api/v1/databases", headers=h)).json()
             if d["db_type"] == "mysql"),
            None,
        )
        if mysql is None:
            check("存在 MySQL 实例", False)
            return 1

        # ---------- 用内置模板 ----------
        print("\n[2] 使用内置 MySQL 备份模板")
        tpl = BACKUP_TEMPLATES["mysql"]["script"]
        check("模板含工具路径解析", "find_tool" in tpl)
        check("模板含 gzip 完整性校验", "gzip -t" in tpl)
        check("模板含保留期清理", "mtime" in tpl)
        check("模板使用 MYSQL_PWD 传口令", "MYSQL_PWD" not in tpl and "{{" in tpl)

        task = await ensure_task(
            client, h,
            {
                "name": "MySQL 真实备份(GitBash)",
                "db_instance_id": mysql["id"],
                "exec_server_id": srv["id"],
                "backup_type": "logical",
                "template_key": "mysql_logical",
                "script": tpl,
                "target_dir": TARGET_DIR,
                "schedule_cron": "",
                "retention_days": 7,
                "timeout_sec": 300,
                "enabled": False,
            },
            "MySQL 真实备份(GitBash)",
        )
        check("任务已创建", bool(task.get("id")), f"id={task.get('id')}")

        # ---------- 执行 ----------
        print("\n[3] 执行真实备份")
        res = (await client.post(
            f"{BASE}/api/v1/backups/{task['id']}/run", headers=h
        )).json()
        out = res.get("output") or ""
        err = res.get("error") or ""
        check("★ 备份执行成功", res.get("ok") is True,
              f"exit={res.get('exit_code')} {err[:120]}")
        for line in (out or err).strip().splitlines()[:8]:
            print("      " + line)

        check("★ gzip 完整性校验通过", "GZIP_OK" in out)
        artifact = str(res.get("artifact") or "")
        check("识别出备份文件路径", bool(artifact), artifact)
        check("记录了耗时", (res.get("duration_ms") or 0) > 0, f"{res.get('duration_ms')}ms")

        # ---------- 落盘校验 ----------
        print("\n[4] 产物落盘校验（Windows 侧）")
        if artifact:
            win = Path("E:/Agent/.tmp/mysql-real") / Path(artifact).name
            check("★ 文件确实存在于磁盘", win.exists(), str(win))
            if win.exists():
                size = win.stat().st_size
                check("★ 文件非空", size > 1024, f"{size:,} 字节")

        # ---------- 历史 ----------
        print("\n[5] 执行历史")
        runs = (await client.get(
            f"{BASE}/api/v1/backups/{task['id']}/runs", headers=h
        )).json()
        check("有执行历史", len(runs.get("runs", [])) >= 1, f"{len(runs.get('runs', []))} 条")
        if runs.get("runs"):
            first = runs["runs"][0]
            check("★ 历史含状态/退出码/产物",
                  first["status"] == "success" and first["exit_code"] == 0
                  and bool(first["artifact"]),
                  f"{first['status']} exit={first['exit_code']}")

    print()
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
        return 1
    print("全部通过：MySQL 备份走通完整平台链路")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
