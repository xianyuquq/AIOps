"""用真实 Kingbase 完成数据库监控的端到端验证。

背景：
  - KingbaseES V9 装在 192.168.203.128，端口 54321
  - 对外 54321 被防火墙拦截（MobaXterm 也连不上），只能从本机访问
    → 因此必须用 SSH 跳板模式，且跳板内连接地址用 127.0.0.1
  - ksql 不在 SSH 会话的 PATH 中，需要指定客户端路径

用法：python tests/verify_kingbase_real.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testenv import (  # noqa: E402
    KINGBASE_DB,
    KINGBASE_HOST,
    KINGBASE_PORT,
    KINGBASE_USER,
    require_kingbase_password,
)

BASE = "http://127.0.0.1:8848"

KINGBASE = {
    "name": "麒麟测试机 Kingbase",
    "db_type": "kingbase",
    "host": KINGBASE_HOST,
    "port": KINGBASE_PORT,
    "username": KINGBASE_USER,
    "secret": require_kingbase_password("verify_kingbase_real.py"),
    "database_name": KINGBASE_DB,
    "collect_mode": "ssh_proxy",
    "proxy_server_id": 5,
    "proxy_host": "127.0.0.1",
    "client_path": "/data/Kingbase/ES/V9/KESRealPro/V009R001C010/Server/bin/ksql",
    "enabled": True,
}

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


async def main() -> int:
    async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
        r = await client.post(
            f"{BASE}/api/v1/auth/login/json",
            json={"username": "admin", "password": "admin123"},
        )
        if r.status_code != 200:
            print(f"登录平台失败：{r.status_code}")
            return 1
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # ---------- 创建或复用实例 ----------
        items = (await client.get(f"{BASE}/api/v1/databases", headers=h)).json()
        existing = next((x for x in items if x["name"] == KINGBASE["name"]), None)
        if existing:
            resp = await client.patch(
                f"{BASE}/api/v1/databases/{existing['id']}", headers=h, json=KINGBASE
            )
            inst = resp.json()
            reused = True
        else:
            resp = await client.post(f"{BASE}/api/v1/databases", headers=h, json=KINGBASE)
            inst = resp.json()
            reused = False
        check("实例已创建/更新", bool(inst.get("id")),
              f"id={inst.get('id')} {'(复用)' if reused else '(新建)'}")
        check("凭据已保存", inst.get("has_secret") is True, str(inst.get("has_secret")))

        iid = inst["id"]

        # ---------- 1. 连接测试 ----------
        print("\n[1] 测试连接（真实握手 + SELECT 1）")
        resp = await client.post(f"{BASE}/api/v1/databases/{iid}/test", headers=h)
        data = resp.json()
        check("★ Kingbase 连接成功", data.get("ok") is True,
              f"{str(data.get('message'))[:100]} ({data.get('duration_ms')}ms)")

        # ---------- 2. 立即采集 ----------
        print("\n[2] 立即采集（跑内置 Kingbase 采集 SQL）")
        resp = await client.post(f"{BASE}/api/v1/databases/{iid}/collect", headers=h)
        col = resp.json()
        check("★ 采集成功", col.get("ok") is True, str(col.get("message", ""))[:80])
        check("指标已落库", (col.get("stored") or 0) > 0, f"写入 {col.get('stored')} 个")

        for item in col.get("results", []):
            if item.get("ok"):
                print(f"      {item['script']}: {item['count']} 个指标  {item['duration_ms']}ms")
                for m in item.get("metrics", []):
                    print(f"         {m['key']:<34} = {m['value']}")
            else:
                print(f"      ✗ {item['script']}: {str(item.get('error'))[:200]}")

        metrics = [m for item in col.get("results", []) if item.get("ok")
                   for m in item.get("metrics", [])]
        check("采到多个指标", len(metrics) >= 5, f"{len(metrics)} 个")

        keys = {m["key"] for m in metrics}
        for expected in ("db.connections.total", "db.connections.max",
                         "db.transactions.committed"):
            check(f"包含关键指标 {expected}", expected in keys)

        # ---------- 3. 指标可按实例查询 ----------
        print("\n[3] 指标归属与查询")
        resp = await client.get(
            f"{BASE}/api/v1/metrics/latest",
            headers=h,
            params={"metric_key": "db.connections.total"},
        )
        latest = resp.json().get("results", [])
        mine = [x for x in latest if x["server_name"] == KINGBASE["name"]]
        check("★ 指标可按实例查询到", bool(mine),
              f"{[(x['server_name'], x['value'], x['target_type']) for x in mine]}")

        # ---------- 4. 启用定时采集 ----------
        print("\n[4] 启用 Kingbase 定时采集脚本")
        scripts = (await client.get(f"{BASE}/api/v1/scripts", headers=h)).json()
        kb = next((s for s in scripts if s["db_type"] == "kingbase"), None)
        check("存在 Kingbase 采集脚本", kb is not None, kb["name"] if kb else "")
        if kb:
            r2 = await client.patch(
                f"{BASE}/api/v1/scripts/{kb['id']}",
                headers=h,
                json={"enabled": True, "interval_sec": 30},
            )
            check("脚本已启用", r2.status_code == 200 and r2.json()["enabled"] is True,
                  f"HTTP {r2.status_code}")

    print()
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
        return 1
    print("全部通过：真实 Kingbase 采集链路可用")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
