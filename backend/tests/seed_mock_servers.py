"""把 mock SSH 节点注册到平台，供浏览器端到端演示使用。

运行前需先启动：
  1. python tests/mock_ssh_servers.py     （4 个模拟节点）
  2. uvicorn app.main:app --port 8848     （平台服务）
"""

from __future__ import annotations

import sys

import httpx

BASE = "http://127.0.0.1:8848"
PORTS = [2501, 2502, 2503, 2504]


def main() -> int:
    with httpx.Client(timeout=20) as client:
        resp = client.post(
            f"{BASE}/api/v1/auth/login/json",
            json={"username": "admin", "password": "admin123"},
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 建一个专用分组
        groups = client.get(f"{BASE}/api/v1/servers/groups", headers=headers).json()
        group = next((g for g in groups if g["name"] == "演示环境"), None)
        if group is None:
            group = client.post(
                f"{BASE}/api/v1/servers/groups",
                headers=headers,
                json={"name": "演示环境", "description": "浏览器端到端演示用 mock 节点"},
            ).json()
        group_id = group["id"]

        existing = client.get(f"{BASE}/api/v1/servers", headers=headers).json()
        existing_hosts = {(s["host"], s["port"]) for s in existing}

        created = 0
        for i, port in enumerate(PORTS, start=1):
            if ("127.0.0.1", port) in existing_hosts:
                print(f"已存在，跳过：127.0.0.1:{port}")
                continue
            resp = client.post(
                f"{BASE}/api/v1/servers",
                headers=headers,
                json={
                    "name": f"web-mock-0{i}",
                    "host": "127.0.0.1",
                    "port": port,
                    "username": "root",
                    "protocol": "ssh",
                    "auth_type": "password",
                    "secret": "mock-password",
                    "group_id": group_id,
                    "tags": {"env": "demo"},
                },
            )
            if resp.status_code >= 400:
                print(f"注册失败 127.0.0.1:{port} -> {resp.status_code} {resp.text}")
                continue
            created += 1
            print(f"已注册 web-mock-0{i} -> 127.0.0.1:{port}")

    print(f"\n完成，新注册 {created} 台。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
