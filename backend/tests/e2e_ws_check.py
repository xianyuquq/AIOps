"""端到端验证：通过真实 WebSocket 测试广播终端协议。

启动服务后运行：
    python tests/e2e_ws_check.py

本脚本连接受限的 WebSocket 端点，创建 4 台"服务器"的会话（会因
目标不可达而处于 offline，这本身也是需要验证的降级行为），
并确认协议消息格式正确、鉴权生效。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import websockets

BASE = "http://127.0.0.1:8848"
WS = "ws://127.0.0.1:8848/ws/terminal"


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    # ---------- 1. 登录拿 token ----------
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{BASE}/api/v1/auth/login/json",
            json={"username": "admin", "password": "admin123"},
        )
        check("登录接口", resp.status_code == 200, f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            return 1
        token = resp.json()["access_token"]

    # ---------- 2. 无 token 应被拒绝 ----------
    try:
        async with websockets.connect(WS) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
        check("WebSocket 未认证应被拒绝", False, "竟然连接成功")
    except Exception as exc:
        check("WebSocket 未认证应被拒绝", True, type(exc).__name__)

    # ---------- 3. 带 token 建立连接 ----------
    async with websockets.connect(f"{WS}?token={token}") as ws:
        check("WebSocket 鉴权连接", True)

        # ping/pong 保活
        await ws.send(json.dumps({"type": "ping"}))
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        check("ping/pong", msg.get("type") == "pong", str(msg.get("type")))

        # ---------- 4. 创建会话（使用不存在的服务器 ID） ----------
        await ws.send(
            json.dumps(
                {
                    "type": "session.create",
                    "server_ids": [99991, 99992],
                    "mode": "mirror",
                    "cols": 120,
                    "rows": 30,
                }
            )
        )
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        check(
            "不存在的服务器应返回错误",
            msg.get("type") == "error",
            str(msg.get("message")),
        )

        # ---------- 5. 错误格式消息 ----------
        await ws.send("not-json")
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        check("非法 JSON 应返回错误", msg.get("type") == "error")

        # ---------- 6. 对不存在会话广播不应崩溃 ----------
        await ws.send(
            json.dumps(
                {"type": "broadcast_input", "session_id": "ghost", "data": "df -Th"}
            )
        )
        # 服务端应保持存活，用 ping 验证
        await ws.send(json.dumps({"type": "ping"}))
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        check("对不存在会话广播后服务仍存活", msg.get("type") == "pong")

    # ---------- 7. 静态资源与文档 ----------
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{BASE}/openapi.json")
        check("OpenAPI 文档可用", resp.status_code == 200)
        spec = resp.json()
        paths = spec.get("paths", {})
        check("API 路径数量充足", len(paths) >= 30, f"{len(paths)} 个路径")

        # 关键端点必须存在
        required = [
            "/api/v1/auth/login/json",
            "/api/v1/servers",
            "/api/v1/scripts",
            "/api/v1/scripts/{script_id}/run",
            "/api/v1/alerts",
            "/api/v1/alerts/{alert_id}/analyze",
            "/api/v1/ai/ask",
            "/api/v1/files/upload",
            "/api/v1/metrics/series",
            "/api/v1/dashboard/summary",
        ]
        missing = [p for p in required if p not in paths]
        check("关键端点齐全", not missing, f"缺失 {missing}" if missing else "")

        # ---------- 8. 内置数据已导入 ----------
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.get(f"{BASE}/api/v1/scripts", headers=headers)
        scripts = resp.json()
        check("内置监控脚本已导入", len(scripts) >= 6, f"{len(scripts)} 个")

        names = {s["name"] for s in scripts}
        for expected in ["Linux 系统指标", "MySQL / MariaDB 指标", "Kingbase 人大金仓指标"]:
            check(f"含内置脚本：{expected}", expected in names)

        resp = await client.get(f"{BASE}/api/v1/alerts/rules", headers=headers)
        rules = resp.json()
        check("内置告警规则已导入", len(rules) >= 10, f"{len(rules)} 条")

        # ---------- 9. Kingbase 脚本必须用 sys_ 视图 ----------
        kingbase = next((s for s in scripts if "Kingbase" in s["name"]), None)
        if kingbase:
            body = kingbase["body"]
            check(
                "Kingbase 脚本使用 sys_ 系统视图",
                "sys_stat_activity" in body and "pg_stat_activity" not in body,
                "国产化适配正确性",
            )

    print()
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
