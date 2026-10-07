"""启动服务器上的 Kingbase 服务（仅用于测试，可用 sys_ctl stop 回退）。

背景：该测试机上 KingbaseES V9 已完成安装与实例初始化
（listen_addresses='*'，sys_hba 允许 0.0.0.0/0 口令认证），
但服务从未启动，也没有 systemd 单元或 init 脚本，因此需要手工拉起。

本脚本只做三件事：
  1. 查看当前状态
  2. 以 kingbase 用户执行 sys_ctl start
  3. 验证 54321 端口是否开始监听

不修改任何配置文件。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.models import Server
from app.services.server_ref import to_ref
from app.services.ssh_pool import pool

SERVER_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 5

KINGBASE_HOME = "/data/Kingbase/ES/V9/KESRealPro/V009R001C010"
DATA_DIR = "/data/Kingbase/ES/V9/data"
LOG_FILE = f"{DATA_DIR}/startup.log"

# 用单引号包裹 su 的命令，内部的 $ 由 kingbase 的 shell 展开
SU_CMD = (
    "su - kingbase -c '"
    f"export KINGBASE_HOME={KINGBASE_HOME}; "
    "export PATH=$KINGBASE_HOME/Server/bin:$PATH; "
    "export LD_LIBRARY_PATH=$KINGBASE_HOME/Server/lib:$LD_LIBRARY_PATH; "
    f"sys_ctl -D {DATA_DIR} -l {LOG_FILE} {{action}}"
    "'"
)


async def main() -> int:
    db = SessionLocal()
    try:
        server = db.get(Server, SERVER_ID)
        if server is None:
            print(f"服务器 {SERVER_ID} 不存在")
            return 1
        ref = to_ref(server)
    finally:
        db.close()

    print(f"目标：{ref.name}  {ref.host}:{ref.port}")
    executor = await pool.get(ref)
    try:
        # ---------- 1. 当前状态 ----------
        print("\n[1] 当前状态")
        r = await executor.run(SU_CMD.format(action="status"), timeout=30)
        print("   " + ((r.stdout or r.stderr or "(无输出)").strip().replace("\n", "\n   ")))

        print("\n[2] 启动前的监听端口")
        r = await executor.run(
            "ss -tln 2>/dev/null | grep -E ':54321' || echo '   54321 未监听'",
            timeout=20,
        )
        print("   " + (r.stdout or "").strip())

        # ---------- 2. 启动 ----------
        print("\n[3] 执行 sys_ctl start")
        r = await executor.run(SU_CMD.format(action="start"), timeout=90)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        print(f"   退出码={r.exit_code}")
        for line in out.splitlines()[:12]:
            print("   " + line)

        # ---------- 3. 等待并验证 ----------
        print("\n[4] 等待服务就绪")
        listening = False
        for i in range(12):
            await asyncio.sleep(2)
            r = await executor.run(
                "ss -tln 2>/dev/null | grep -E ':54321' | head -3", timeout=20
            )
            if (r.stdout or "").strip():
                print(f"   第 {i * 2 + 2}s：54321 已监听")
                print("   " + r.stdout.strip())
                listening = True
                break
            print(f"   第 {i * 2 + 2}s：尚未监听…")

        if not listening:
            print("\n   ⚠ 端口仍未监听，查看启动日志：")
            r = await executor.run(f"tail -30 {LOG_FILE} 2>/dev/null", timeout=20)
            print("   " + (r.stdout or "(无日志)").strip().replace("\n", "\n   "))

        print("\n[5] 进程确认")
        r = await executor.run(
            "ps -ef | grep -E 'kingbase' | grep -v grep | head -8", timeout=20
        )
        print("   " + ((r.stdout or "(无 kingbase 进程)").strip().replace("\n", "\n   ")))
    finally:
        await pool.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
