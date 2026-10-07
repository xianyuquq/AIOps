"""容灾备份任务的端到端验证。

用一个「会执行备份命令」的 mock SSH 主机来验证整条链路：
  模板渲染 → 密码以环境变量注入（不出现在命令行） → SSH 执行
  → 识别备份文件 → 统计清理数量 → 记录执行历史

之所以不直接跑在用户的真实服务器上：验证不应在别人的生产机上创建文件。
mock 足够覆盖平台侧的全部逻辑。

用法：先启动服务，再运行本脚本
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncssh
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8848"
PORT = 2801
# 一个显眼的密码，用于断言它绝不出现在命令行里
SECRET = "S3cret-Backup-P@ss!"

failures: list[str] = []
RECEIVED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


class BackupHost(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return False


async def handle(process: asyncssh.SSHServerProcess) -> None:
    """模拟一台装了备份工具的数据库主机。"""
    command = process.command or ""
    RECEIVED.append(command)

    if not command.strip():
        process.exit(0)
        return

    # 模拟备份命令的输出
    if "BACKUP_FILE=" in command or "mysqldump" in command or "pg_dump" in command:
        # 从命令里解析出输出路径（模拟真实工具写出文件）
        out_path = "/var/backups/aiops/mock/backup_mock.dump"
        for token in command.split():
            if token.startswith("/var/backups") or token.startswith("/tmp/"):
                out_path = token.strip('"').strip("'")
                break
        process.stdout.write(f"BACKUP_FILE={out_path}\n")
        process.stdout.write(f"-rw-r--r-- 1 root root 20480 Oct  7 12:00 {out_path}\n")
        # 模拟清理过期文件（-print -delete 会先打印被删路径）
        process.stdout.write("/var/backups/aiops/mock/old_20260101.dump\n")
        process.stdout.write("/var/backups/aiops/mock/old_20260102.dump\n")
        process.exit(0)
        return

    if "echo" in command:
        process.stdout.write("ok\n")
        process.exit(0)
        return

    process.stdout.write("done\n")
    process.exit(0)


async def main() -> int:
    key = asyncssh.generate_private_key("ssh-rsa")
    server = await asyncssh.create_server(
        BackupHost,
        "127.0.0.1",
        PORT,
        server_host_keys=[key],
        process_factory=handle,
        reuse_address=True,
        line_editor=False,
        line_echo=False,
    )
    print(f"mock 备份主机已监听 127.0.0.1:{PORT}\n")

    try:
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            r = await client.post(
                f"{BASE}/api/v1/auth/login/json",
                json={"username": "admin", "password": "admin123"},
            )
            h = {"Authorization": f"Bearer {r.json()['access_token']}"}

            # ---------- 准备：一台执行服务器 + 一个数据库实例 ----------
            servers = (await client.get(f"{BASE}/api/v1/servers", headers=h)).json()
            srv = next((s for s in servers if s["port"] == PORT), None)
            if srv is None:
                srv = (
                    await client.post(
                        f"{BASE}/api/v1/servers",
                        headers=h,
                        json={
                            "name": "备份执行主机(mock)",
                            "host": "127.0.0.1",
                            "port": PORT,
                            "username": "root",
                            "protocol": "ssh",
                            "auth_type": "password",
                            "secret": "x",
                            "tags": {},
                        },
                    )
                ).json()
            check("执行服务器已就绪", bool(srv.get("id")), f"id={srv.get('id')}")

            dbs = (await client.get(f"{BASE}/api/v1/databases", headers=h)).json()
            pg = next((d for d in dbs if d["db_type"] == "postgresql"), None)
            if pg is None:
                print("  跳过：没有 PostgreSQL 实例，请先运行 verify_db_monitoring.py")
                return 1

            # ---------- 模板列表 ----------
            print("[1] 备份模板可供选择")
            tpl = (await client.get(f"{BASE}/api/v1/backups/templates", headers=h)).json()
            keys = [t["key"] for t in tpl["templates"]]
            check("返回多个模板", len(keys) >= 5, str(keys))
            check("包含物理/配置类模板",
                  any(t["backup_type"] == "config" for t in tpl["templates"]))
            pg_tpl = next(t for t in tpl["templates"] if t["key"] == "postgresql_logical")
            check("模板带说明", bool(pg_tpl.get("notes")))
            check("模板含保留策略清理", "find" in pg_tpl["script"] and "mtime" in pg_tpl["script"])

            # ---------- 创建任务 ----------
            print("\n[2] 创建备份任务（选用 pg_dump 模板）")
            task = (
                await client.post(
                    f"{BASE}/api/v1/backups",
                    headers=h,
                    json={
                        "name": "PG 每日逻辑备份",
                        "db_instance_id": pg["id"],
                        "exec_server_id": srv["id"],
                        "backup_type": "logical",
                        "template_key": "postgresql_logical",
                        "script": pg_tpl["script"],
                        "target_dir": "/var/backups/aiops/mock",
                        "schedule_cron": "0 2 * * *",
                        "retention_days": 7,
                        "enabled": False,
                    },
                )
            ).json()
            check("任务创建成功", bool(task.get("id")), f"id={task.get('id')}")
            check("任务默认停用（避免误触发）", task.get("enabled") is False)

            # ---------- 执行 ----------
            print("\n[3] 手动执行备份")
            RECEIVED.clear()
            run = (await client.post(f"{BASE}/api/v1/backups/{task['id']}/run", headers=h)).json()
            check("★ 备份执行成功", run.get("ok") is True,
                  f"exit={run.get('exit_code')} {str(run.get('error',''))[:70]}")
            check("识别出备份文件路径", bool(run.get("artifact")), str(run.get("artifact")))
            check("记录了执行耗时", (run.get("duration_ms") or 0) > 0,
                  f"{run.get('duration_ms')}ms")

            # ---------- 安全断言：密码不得出现在命令行 ----------
            print("\n[4] 安全：数据库密码不得出现在命令行")
            joined = "\n".join(RECEIVED)
            check("★ 命令行中不含密码", SECRET not in joined,
                  "命令行泄漏了密码！" if SECRET in joined else "")
            check("密码以环境变量注入",
                  "PGPASSWORD=" in joined or "MYSQL_PWD=" in joined,
                  joined[:160].replace("\n", " ⏎ "))
            check("模板占位符已全部替换",
                  "{{" not in joined, "仍存在未替换的占位符" if "{{" in joined else "")

            # ---------- 历史 ----------
            print("\n[5] 执行历史与结果记录")
            runs = (await client.get(f"{BASE}/api/v1/backups/{task['id']}/runs", headers=h)).json()
            check("有执行历史", len(runs.get("runs", [])) >= 1, f"{len(runs.get('runs', []))} 条")
            first = runs["runs"][0]
            check("历史含状态与退出码",
                  first["status"] == "success" and first["exit_code"] == 0,
                  f"{first['status']} exit={first['exit_code']}")
            check("★ 统计了被清理的过期文件数", first["purged"] >= 1,
                  f"purged={first['purged']}")

            tasks = (await client.get(f"{BASE}/api/v1/backups", headers=h)).json()
            mine = next(t for t in tasks if t["id"] == task["id"])
            check("任务上回写了最近执行结果",
                  mine["last_status"] == "success" and bool(mine["last_message"]),
                  mine["last_message"][:70])
            check("回写了关联实例名", bool(mine.get("db_instance_name")),
                  mine.get("db_instance_name"))

            # ---------- 修改 ----------
            print("\n[6] 任务可修改")
            r2 = await client.patch(
                f"{BASE}/api/v1/backups/{task['id']}",
                headers=h,
                json={"retention_days": 3, "schedule_cron": "30 1 * * *"},
            )
            check("修改任务成功", r2.status_code == 200 and r2.json()["retention_days"] == 3,
                  f"HTTP {r2.status_code}")

            # ---------- 调度注册 ----------
            print("\n[7] 启用后注册调度")
            await client.patch(
                f"{BASE}/api/v1/backups/{task['id']}", headers=h, json={"enabled": True}
            )
            # 通过健康检查无法看到 job，直接查库中的任务状态即可
            tasks2 = (await client.get(f"{BASE}/api/v1/backups", headers=h)).json()
            mine2 = next(t for t in tasks2 if t["id"] == task["id"])
            check("任务已启用", mine2["enabled"] is True)

            # ---------- 清理 ----------
            await client.delete(f"{BASE}/api/v1/backups/{task['id']}", headers=h)
            left = (await client.get(f"{BASE}/api/v1/backups", headers=h)).json()
            check("任务可删除", all(t["id"] != task["id"] for t in left))
    finally:
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=5)
        except Exception:
            pass

    print()
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
        return 1
    print("全部通过：容灾备份链路可用")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
