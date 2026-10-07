"""真实数据库的备份容灾端到端验证（Kingbase，走完整平台链路）。

分工说明：
- verify_backup.py     —— mock 主机，验证平台侧编排逻辑
- verify_backup_real.py—— 本脚本：**真的调 sys_dump 产出备份文件**

为什么只覆盖 Kingbase：
  备份命令是 POSIX shell 语法，而本机是 Windows —— 没有可用的 POSIX shell
  （无 Git Bash，WSL 未安装发行版）。因此 MySQL 无法在本机通过 SSH 跑通平台链路，
  它的命令模板改用 PowerShell 直接验证（见 verify_mysql_backup.ps1 说明）。

Kingbase 这条链路是**完整真实**的：
  平台 → SSH 到麒麟服务器 → sys_dump → 真实 .dump 文件 → sys_restore 校验

用法：python tests/verify_backup_real.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8848"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


async def ensure_backup(client, h, payload: dict, name: str) -> dict:
    items = (await client.get(f"{BASE}/api/v1/backups", headers=h)).json()
    for t in items:
        if t["name"] == name:
            await client.patch(f"{BASE}/api/v1/backups/{t['id']}", headers=h, json=payload)
            return t
    return (await client.post(f"{BASE}/api/v1/backups", headers=h, json=payload)).json()


async def main() -> int:
    from app.builtin_scripts.backup_templates import BACKUP_TEMPLATES

    async with httpx.AsyncClient(timeout=900, trust_env=False) as client:
        r = await client.post(
            f"{BASE}/api/v1/auth/login/json",
            json={"username": "admin", "password": "admin123"},
        )
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        dbs = (await client.get(f"{BASE}/api/v1/databases", headers=h)).json()
        kingbase = next((d for d in dbs if d["db_type"] == "kingbase"), None)
        if kingbase is None:
            check("存在 Kingbase 实例", False)
            return 1

        servers = (await client.get(f"{BASE}/api/v1/servers", headers=h)).json()
        kb_server = next(
            (s for s in servers if s["host"] == kingbase["host"] and s["port"] == 22), None
        )
        if kb_server is None:
            check("找到 Kingbase 所在服务器", False)
            return 1

        print("[1] 准备")
        check("Kingbase 实例就绪", True, f"{kingbase['name']} ({kingbase['db_type']})")
        check("执行服务器（麒麟）就绪", True, kb_server["name"])

        t = (await client.post(
            f"{BASE}/api/v1/databases/{kingbase['id']}/test", headers=h
        )).json()
        check("数据库连接正常", t.get("ok") is True, str(t.get("message"))[:60])

        # ---------- 用内置模板（用户实际拿到的那个） ----------
        print("\n[2] 使用内置 Kingbase 备份模板创建任务")
        tpl = BACKUP_TEMPLATES["kingbase"]["script"]
        check("模板使用 stdin 喂口令（而非 PGPASSWORD）",
              "printf" in tpl and "-W" in tpl,
              "PGPASSWORD 在金仓上无效，会挂起")
        check("模板含归档校验", "SYSRESTORE" in tpl and "-l " in tpl)
        check("模板含保留期清理", "mtime" in tpl)

        task = await ensure_backup(
            client, h,
            {
                "name": "Kingbase 真实备份(内置模板)",
                "db_instance_id": kingbase["id"],
                "exec_server_id": kb_server["id"],
                "backup_type": "logical",
                "template_key": "kingbase_logical",
                "script": tpl,
                "target_dir": "/var/backups/aiops/realtest",
                "schedule_cron": "",
                "retention_days": 7,
                "timeout_sec": 300,
                "enabled": False,
            },
            "Kingbase 真实备份(内置模板)",
        )
        check("任务已创建", bool(task.get("id")), f"id={task.get('id')}")

        # ---------- 执行 ----------
        print("\n[3] 执行真实备份")
        result = (await client.post(
            f"{BASE}/api/v1/backups/{task['id']}/run", headers=h
        )).json()
        check("★ 备份执行成功", result.get("ok") is True,
              f"exit={result.get('exit_code')} {str(result.get('error'))[:140]}")
        out = result.get("output") or ""
        err = result.get("error") or ""
        for line in (out or err).strip().splitlines()[:10]:
            print(f"      {line}")

        check("★ 归档可读（sys_restore -l 通过）", "ARCHIVE_OK" in out)
        check("识别出备份文件路径", bool(result.get("artifact")), str(result.get("artifact")))
        check("备份文件路径在目标目录下",
              str(result.get("artifact", "")).startswith("/var/backups/aiops/realtest"),
              str(result.get("artifact")))
        check("记录了执行耗时", (result.get("duration_ms") or 0) > 0,
              f"{result.get('duration_ms')}ms")

        # ---------- 历史 ----------
        print("\n[4] 执行历史与任务回写")
        runs = (await client.get(
            f"{BASE}/api/v1/backups/{task['id']}/runs", headers=h
        )).json()
        check("有执行历史", len(runs.get("runs", [])) >= 1, f"{len(runs.get('runs', []))} 条")
        if runs.get("runs"):
            first = runs["runs"][0]
            check("★ 历史含状态/退出码/产物",
                  first["status"] == "success" and first["exit_code"] == 0
                  and bool(first["artifact"]),
                  f"{first['status']} exit={first['exit_code']} artifact={first['artifact']}")

        tasks = (await client.get(f"{BASE}/api/v1/backups", headers=h)).json()
        mine = next((x for x in tasks if x["id"] == task["id"]), None)
        check("任务回写了最近结果", bool(mine and mine["last_message"]),
              str(mine["last_message"] if mine else "")[:80])

    print()
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
        return 1
    print("全部通过：真实 Kingbase 备份链路可用")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
