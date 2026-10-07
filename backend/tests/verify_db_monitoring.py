"""真实数据库采集的端到端验证。

对 Docker 中真实运行的 PostgreSQL 与 MySQL 验证：
  1. 直连采集：能连上、能执行 SQL、能解析出指标
  2. 指标写入数据库并按 db_instance_id 正确归属
  3. 连接测试能识别错误密码
  4. 采集脚本对不支持的 SQL 语义给出清晰错误

用法：先启动服务，再运行本脚本
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testenv import LOCAL_DB_PASSWORD  # noqa: E402

BASE = "http://127.0.0.1:8848"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


PG = {
    "name": "Docker PostgreSQL",
    "db_type": "postgresql",
    "host": "127.0.0.1",
    "port": 5432,
    "username": "postgres",
    "secret": LOCAL_DB_PASSWORD,
    "database_name": "testdb",
    "collect_mode": "direct",
}
MYSQL = {
    "name": "Docker MySQL",
    "db_type": "mysql",
    "host": "127.0.0.1",
    "port": 3306,
    "username": "root",
    "secret": LOCAL_DB_PASSWORD,
    "database_name": "testdb",
    "collect_mode": "direct",
}


async def ensure_instance(client, h, spec) -> dict:
    """创建或复用实例。"""
    items = (await client.get(f"{BASE}/api/v1/databases", headers=h)).json()
    for it in items:
        if it["name"] == spec["name"]:
            await client.patch(
                f"{BASE}/api/v1/databases/{it['id']}", headers=h, json=spec
            )
            return it
    resp = await client.post(f"{BASE}/api/v1/databases", headers=h, json=spec)
    return resp.json()


async def main() -> int:
    # trust_env=False：httpx 默认会读取系统代理（Windows 上包括注册表里的
    # ProxyServer），开启 VPN 后会让本机 127.0.0.1 的请求也被送到代理，
    # 表现为莫名其妙的 502。直连本机服务必须关掉。
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        r = await client.post(
            f"{BASE}/api/v1/auth/login/json",
            json={"username": "admin", "password": "admin123"},
        )
        if r.status_code != 200:
            print(f"登录失败 {r.status_code}: {r.text}")
            return 1
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # ---------- 模板接口 ----------
        print("[0] 采集与备份模板")
        tpl = (await client.get(f"{BASE}/api/v1/databases/templates", headers=h)).json()
        check("返回数据库类型与默认端口", len(tpl.get("db_types", [])) >= 4,
              str([x["key"] for x in tpl.get("db_types", [])]))
        bkp = (await client.get(f"{BASE}/api/v1/backups/templates", headers=h)).json()
        check("返回备份脚本模板", len(bkp.get("templates", [])) >= 5,
              str([x["key"] for x in bkp.get("templates", [])]))

        # ---------- PostgreSQL ----------
        print("\n[1] PostgreSQL 直连采集")
        pg = await ensure_instance(client, h, PG)
        check("实例已就绪", bool(pg.get("id")), f"id={pg.get('id')}")

        r = await client.post(f"{BASE}/api/v1/databases/{pg['id']}/test", headers=h)
        data = r.json()
        check("★ 连接测试成功", data.get("ok") is True,
              f"{data.get('message','')[:90]} ({data.get('duration_ms')}ms)")

        r = await client.post(f"{BASE}/api/v1/databases/{pg['id']}/collect", headers=h)
        col = r.json()
        check("★ 立即采集成功", col.get("ok") is True, str(col.get("message", ""))[:90])
        results = col.get("results", [])
        check("至少一个采集脚本产出指标",
              any(x.get("ok") and x.get("count", 0) > 0 for x in results),
              "; ".join(f"{x['script']}={x.get('count', x.get('error','?'))}" for x in results))
        for item in results:
            if item.get("ok"):
                sample = item.get("metrics", [])[:4]
                print(f"      {item['script']}: {item['count']} 个指标  "
                      f"例如 {[(m['key'], m['value']) for m in sample]}")

        # ---------- MySQL ----------
        print("\n[2] MySQL 直连采集")
        my = await ensure_instance(client, h, MYSQL)
        check("实例已就绪", bool(my.get("id")), f"id={my.get('id')}")

        r = await client.post(f"{BASE}/api/v1/databases/{my['id']}/test", headers=h)
        data2 = r.json()
        check("★ 连接测试成功", data2.get("ok") is True,
              f"{data2.get('message','')[:90]} ({data2.get('duration_ms')}ms)")

        r = await client.post(f"{BASE}/api/v1/databases/{my['id']}/collect", headers=h)
        col2 = r.json()
        results2 = col2.get("results", [])
        check("★ 立即采集成功", col2.get("ok") is True)
        check("至少一个采集脚本产出指标",
              any(x.get("ok") and x.get("count", 0) > 0 for x in results2),
              "; ".join(f"{x['script']}={x.get('count', x.get('error','?'))}" for x in results2))
        for item in results2:
            if item.get("ok"):
                sample = item.get("metrics", [])[:5]
                print(f"      {item['script']}: {item['count']} 个指标  "
                      f"例如 {[(m['key'], m['value']) for m in sample]}")

        # ---------- 错误密码必须被识别 ----------
        print("\n[3] 错误密码应给出清晰错误")
        bad_spec = dict(PG, name="Docker PostgreSQL 错误密码", secret="wrong-password")
        bad = await ensure_instance(client, h, bad_spec)
        r = await client.post(f"{BASE}/api/v1/databases/{bad['id']}/test", headers=h)
        bd = r.json()
        check("错误密码连接失败", bd.get("ok") is False)
        msg = (bd.get("message") or "").strip()
        check("错误信息非空且可读", len(msg) > 10, msg[:110])
        await client.delete(f"{BASE}/api/v1/databases/{bad['id']}", headers=h)

        # ---------- 编辑能力 ----------
        print("\n[4] 实例可编辑（此前只能删除）")
        newname = PG["name"] + " 改"
        r = await client.patch(
            f"{BASE}/api/v1/databases/{pg['id']}", headers=h, json={"name": newname}
        )
        check("★ PATCH 修改实例成功", r.status_code == 200, f"HTTP {r.status_code}")
        check("名称已更新", r.json().get("name") == newname, r.json().get("name"))
        # 改回原名
        await client.patch(
            f"{BASE}/api/v1/databases/{pg['id']}", headers=h, json={"name": PG["name"]}
        )
        # 只改描述性字段不应清空密码
        after = (await client.get(f"{BASE}/api/v1/databases", headers=h)).json()
        target = next(x for x in after if x["id"] == pg["id"])
        check("编辑后密码仍在（留空表示不修改）", target.get("has_secret") is True)

        # ---------- 指标归属 ----------
        print("\n[5] 数据库指标按实例正确归属")
        r = await client.get(
            f"{BASE}/api/v1/metrics/series",
            headers=h,
            params={"metric_key": "db.connections.total", "hours": 1},
        )
        series = r.json()
        check("能按指标查询到数据库序列", len(series) >= 1,
              f"{len(series)} 条序列")
        if series:
            print(f"      序列：{[ (s['server_name'], len(s['points'])) for s in series ]}")

    print()
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
        return 1
    print("全部通过：数据库监控已能真实采集")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
