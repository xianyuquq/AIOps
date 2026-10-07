"""诊断到目标服务器的 SSH 连接：定位失败发生在哪个阶段。

不会使用真实凭据，只用一个不存在的用户名探测算法协商是否正常。
避免多次尝试导致账号被锁。

用法：python tests/diagnose_ssh.py <host> [port]
"""

from __future__ import annotations

import asyncio
import socket
import sys

import asyncssh


async def diagnose(host: str, port: int = 22) -> None:
    print("=" * 62)
    print(f"SSH 连接诊断：{host}:{port}")
    print("=" * 62)

    # ---------- 阶段 1：TCP ----------
    print("\n[1/4] TCP 可达性")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=6
        )
        banner = await asyncio.wait_for(reader.readline(), timeout=6)
        print(f"      ✓ TCP 已连通")
        print(f"      ✓ 服务端 Banner: {banner.decode(errors='replace').strip()}")
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    except asyncio.TimeoutError:
        print("      ✗ 连接超时 —— 防火墙拦截或端口未开放")
        return
    except OSError as exc:
        print(f"      ✗ TCP 失败：{exc}")
        return

    # ---------- 阶段 2：本机 DNS / 路由 ----------
    print("\n[2/4] 地址解析")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        addrs = sorted({i[4][0] for i in infos})
        print(f"      ✓ 解析为 {addrs}")
    except OSError as exc:
        print(f"      ✗ 解析失败：{exc}")

    # ---------- 阶段 3：算法协商（用不存在的用户，避免锁账号）----------
    print("\n[3/4] SSH 算法协商（使用不存在的用户名探测，不会影响真实账号）")
    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(
                host,
                port=port,
                username="__aiops_probe_nonexistent__",
                password="__probe__",
                known_hosts=None,
                connect_timeout=10,
            ),
            timeout=20,
        )
        print("      ! 意外连接成功（服务端可能允许免密）")
        conn.close()
    except asyncssh.PermissionDenied as exc:
        print(f"      ✓ 算法协商成功（服务端正常拒绝了这个不存在的用户）")
        print(f"        返回：{exc}")
    except asyncssh.KeyExchangeFailed as exc:
        print(f"      ✗ 密钥交换失败（算法不兼容）：{exc}")
    except asyncssh.HostKeyNotVerifiable as exc:
        print(f"      ✗ 主机密钥校验失败：{exc}")
    except (OSError, asyncssh.Error) as exc:
        print(f"      ✗ 连接失败：{type(exc).__name__}: {exc}")
    except asyncio.TimeoutError:
        print("      ✗ 协商超时")

    # ---------- 阶段 4：列出支持的算法 ----------
    print("\n[4/4] 客户端（asyncssh）支持的算法")
    try:
        from asyncssh import get_kex_algs, get_server_host_key_algs

        kex = list(get_kex_algs())[:6]
        hk = list(get_server_host_key_algs())[:6]
        print(f"      密钥交换: {kex}")
        print(f"      主机密钥: {hk}")
    except Exception as exc:
        print(f"      （无法枚举：{exc}）")

    print("\n" + "=" * 62)
    print("结论：若 [1][2][3] 全部 ✓，说明网络与算法都没问题，")
    print("      连接失败的原因是凭据（用户名/密码/私钥）或认证方式。")
    print("=" * 62)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python tests/diagnose_ssh.py <host> [port]")
        sys.exit(1)
    asyncio.run(diagnose(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 22))
