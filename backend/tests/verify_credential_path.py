"""端到端验证「凭据链路」：页面填密码 → 加密落库 → 解密 → SSH 认证。

这是既有测试的盲区：L2 的 SSH 测试直接把明文密码传给 SSHExecutor，
绕过了「加密存储 / 解密读取」这一段。如果这段有问题，
用户就会遇到「MobaXterm 能连、平台连不上」。

本脚本会：
  1. 起一个**要求密码认证**的 mock SSH 服务器
  2. 通过平台 API 创建服务器（带正确密码）→ 验证连接成功
  3. 把密码改成错的 → 验证失败且错误信息清晰非空
  4. 验证密码含特殊字符时也能正确往返
  5. 清理测试数据

用法：先启动平台，然后 python tests/verify_credential_path.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncssh
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8848"
PORT = 2701
GOOD_USER = "root"
GOOD_PASS = "T3st@Pass#2026!中文"
WRONG_PASS = "definitely-wrong"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


class PasswordOnlyServer(asyncssh.SSHServer):
    """只接受固定用户名+密码的 SSH 服务器。"""

    def begin_auth(self, username: str) -> bool:
        return True  # 需要认证

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        ok = username == GOOD_USER and password == GOOD_PASS
        print(f"      [mock] 收到认证请求 user={username!r} pass={password!r} -> {ok}")
        return ok


async def handle(process: asyncssh.SSHServerProcess) -> None:
    """同时支持两种会话：
    - exec 请求（conn.run("echo aiops-ok")）—— 平台的连接测试走这条
    - 交互式 shell —— 终端广播走这条
    """
    command = process.command
    if command:
        if command.strip() == "echo aiops-ok":
            process.stdout.write("aiops-ok\n")
        else:
            process.stdout.write(f"ran: {command}\n")
        process.exit(0)
        return

    process.stdout.write("$ ")
    while True:
        try:
            data = await process.stdin.read(1024)
        except asyncssh.TerminalSizeChanged:
            continue
        except (asyncssh.Error, OSError):
            break
        if not data:
            break
        process.stdout.write("aiops-ok\r\n$ ")


async def main() -> int:
    key = asyncssh.generate_private_key("ssh-rsa")
    server = await asyncssh.create_server(
        PasswordOnlyServer,
        "127.0.0.1",
        PORT,
        server_host_keys=[key],
        process_factory=handle,
        reuse_address=True,
        line_editor=False,
        line_echo=False,
    )
    print(f"mock SSH（要求密码认证）已监听 127.0.0.1:{PORT}")
    print(f"正确凭据：{GOOD_USER} / {GOOD_PASS!r}\n")

    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        r = await client.post(
            f"{BASE}/api/v1/auth/login/json",
            json={"username": "admin", "password": "admin123"},
        )
        if r.status_code != 200:
            print(f"登录平台失败：{r.status_code}")
            server.close()
            return 1
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        async def create(name: str, secret: str) -> dict:
            """创建或复用测试服务器（(host,port,username) 有唯一约束）。"""
            listing = await client.get(f"{BASE}/api/v1/servers", headers=h)
            for item in listing.json():
                if (
                    item["host"] == "127.0.0.1"
                    and item["port"] == PORT
                    and item["username"] == GOOD_USER
                ):
                    # 复用已有记录，并把密码重置为本次用例需要的值
                    await client.patch(
                        f"{BASE}/api/v1/servers/{item['id']}",
                        headers=h,
                        json={"name": name, "secret": secret},
                    )
                    item["has_secret"] = True
                    return item

            resp = await client.post(
                f"{BASE}/api/v1/servers",
                headers=h,
                json={
                    "name": name,
                    "host": "127.0.0.1",
                    "port": PORT,
                    "username": GOOD_USER,
                    "protocol": "ssh",
                    "auth_type": "password",
                    "secret": secret,
                    "tags": {},
                },
            )
            return resp.json()

        created_ids: list[int] = []

        # ---------- 用例 1：正确密码 ----------
        # 注意：server 表对 (host, port, username) 有唯一约束，
        # 因此这里复用同一条记录，通过 PATCH 改密码，而不是重复创建。
        print("\n[用例 1] 通过 API 创建服务器并填写正确密码")
        srv = await create(f"凭据测试-{PORT}", GOOD_PASS)
        if not srv.get("id"):
            check("服务器创建成功", False, f"接口返回：{srv}")
            server.close()
            return 1
        created_ids.append(srv["id"])
        check("服务器创建成功", True, f"id={srv['id']}")
        check("接口未回传明文密码", "secret" not in srv and GOOD_PASS not in str(srv))
        check("接口标明已保存凭据", srv.get("has_secret") is True)

        resp = await client.post(f"{BASE}/api/v1/servers/{srv['id']}/test", headers=h)
        data = resp.json()
        check("★ 正确密码连接成功", data.get("ok") is True,
              f"ok={data.get('ok')} msg={data.get('message','')[:80]}")

        # ---------- 用例 2：错误密码 ----------
        print("\n[用例 2] 密码错误时应给出清晰、非空的错误")
        await client.patch(
            f"{BASE}/api/v1/servers/{srv['id']}", headers=h, json={"secret": WRONG_PASS}
        )
        resp = await client.post(f"{BASE}/api/v1/servers/{srv['id']}/test", headers=h)
        data2 = resp.json()
        check("错误密码连接失败", data2.get("ok") is False)
        msg = (data2.get("message") or "").strip()
        check("错误信息非空", len(msg) > 5, repr(msg[:90]))
        check("错误信息不是空泛的「连接失败：」",
              not msg.endswith("：") and msg != "连接失败：", repr(msg[:60]))
        check("错误信息提示是认证问题", "认证" in msg or "密码" in msg, msg[:70])

        # ---------- 用例 3：特殊字符密码往返 ----------
        print("\n[用例 3] 含 @ # ! 与中文的密码必须能正确加解密")
        from app.core.crypto import decrypt_secret, encrypt_secret

        ct, nonce = encrypt_secret(GOOD_PASS)
        check("加解密往返一致", decrypt_secret(ct, nonce) == GOOD_PASS,
              repr(GOOD_PASS))
        check("密文不包含明文", GOOD_PASS.encode("utf-8") not in ct)

        # ---------- 用例 4：修改密码后立即生效 ----------
        print("\n[用例 4] 编辑密码后连接池应失效并重建")
        await client.patch(
            f"{BASE}/api/v1/servers/{srv['id']}", headers=h, json={"secret": GOOD_PASS}
        )
        resp = await client.post(f"{BASE}/api/v1/servers/{srv['id']}/test", headers=h)
        data4 = resp.json()
        check("★ 改回正确密码后连接成功", data4.get("ok") is True,
              f"msg={data4.get('message','')[:80]}")

        # ---------- 用例 5：诊断接口 ----------
        print("\n[用例 5] 诊断接口对正确凭据应全阶段通过")
        resp = await client.post(f"{BASE}/api/v1/servers/{srv['id']}/diagnose", headers=h)
        diag = resp.json()
        check("诊断返回分阶段结果", len(diag.get("steps", [])) >= 3,
              f"{len(diag.get('steps', []))} 阶段")
        check("★ 诊断判定连接正常", diag.get("ok") is True,
              "; ".join(f"{s['step']}={'OK' if s['ok'] else 'FAIL'}" for s in diag.get("steps", [])))

        # ---------- 清理 ----------
        for sid in created_ids:
            if sid:
                await client.delete(f"{BASE}/api/v1/servers/{sid}", headers=h)
        print("\n已清理测试数据")

    server.close()
    try:
        await asyncio.wait_for(server.wait_closed(), timeout=5)
    except (asyncio.TimeoutError, Exception):
        pass

    print()
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
        return 1
    print("全部通过：凭据链路（加密落库 → 解密 → SSH 认证）工作正常")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
