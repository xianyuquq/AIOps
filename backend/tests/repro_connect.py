"""用平台自身的代码路径复现到目标服务器的连接失败，打印真实异常。

使用不存在的用户名，避免影响真实账号或触发锁定。

用法：python tests/repro_connect.py <host> [port] [username]
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncssh

from app.services.server_ref import ServerRef
from app.services.ssh_pool import SSHExecutor, _build_connect_kwargs


async def main(host: str, port: int, username: str) -> None:
    ref = ServerRef(
        id=999, name="probe", host=host, port=port,
        username=username, auth_type="password",
    )

    # ---------- 1) 直接看平台构造的连接参数 ----------
    kwargs = _build_connect_kwargs(ref, "dummy-password")
    print("平台实际使用的连接参数：")
    for k, v in kwargs.items():
        print(f"    {k} = {v!r}")

    # ---------- 2) 走平台的 SSHExecutor ----------
    print("\n>>> 调用 SSHExecutor.connect()")
    executor = SSHExecutor(ref.id, ref.name, ref, "dummy-password")
    try:
        await executor.connect()
        print("    意外连接成功")
    except Exception as exc:
        print(f"    异常类型 : {type(exc).__module__}.{type(exc).__name__}")
        print(f"    str(exc) : {str(exc)!r}")
        print(f"    repr     : {repr(exc)}")
        cause = exc.__cause__
        if cause is not None:
            print(f"    原始异常 : {type(cause).__name__}: {str(cause)!r}")
        print("\n    完整调用栈：")
        traceback.print_exc()

    # ---------- 3) 对照：去掉 preferred_auth 再试一次 ----------
    print("\n>>> 对照实验：不使用 preferred_auth")
    try:
        await asyncio.wait_for(
            asyncssh.connect(
                host, port=port, username=username, password="dummy-password",
                known_hosts=None, connect_timeout=10,
            ),
            timeout=20,
        )
        print("    意外连接成功")
    except Exception as exc:
        print(f"    异常类型 : {type(exc).__name__}")
        print(f"    str(exc) : {str(exc)!r}")

    await executor.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python tests/repro_connect.py <host> [port] [username]")
        sys.exit(1)
    h = sys.argv[1]
    p = int(sys.argv[2]) if len(sys.argv) > 2 else 22
    u = sys.argv[3] if len(sys.argv) > 3 else "__aiops_probe_nonexistent__"
    asyncio.run(main(h, p, u))
