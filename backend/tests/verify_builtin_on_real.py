"""在真实服务器上验证内置监控脚本（当前指向麒麟 Kylin V10）。

重点验证：中文 locale 环境下，指标解析是否依然正确。
很多采集脚本在中文系统上会因为 df/free 输出被翻译而解析失败。

用法：python tests/verify_builtin_on_real.py [server_id]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.models import MetricScript, Server
from app.services.script_runner import execute_script
from app.services.server_ref import to_ref
from app.services.ssh_pool import pool

SERVER_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 5


async def main() -> int:
    db = SessionLocal()
    try:
        server = db.get(Server, SERVER_ID)
        if server is None:
            print(f"服务器 {SERVER_ID} 不存在")
            return 1
        ref = to_ref(server)
        script = (
            db.query(MetricScript)
            .filter(MetricScript.name.like("%Linux%"))
            .first()
        )
        if script is None:
            print("未找到内置 Linux 监控脚本")
            return 1
        script_name = script.name
        body = script.body
    finally:
        db.close()

    print(f"目标：{ref.name}  {ref.host}:{ref.port}（{ref.username}）")
    print(f"脚本：{script_name}")
    print("=" * 68)

    executor = await pool.get(ref)
    try:
        # ---------- 先看原始环境信息 ----------
        env = await executor.run(
            "echo \"LANG=$LANG  LC_ALL=$LC_ALL\"; "
            "cat /etc/os-release 2>/dev/null | head -2; "
            "uname -r",
            timeout=20,
        )
        print("远端环境：")
        for line in (env.stdout or "").strip().splitlines():
            print("   ", line)
        print()

        # ---------- 执行内置采集脚本 ----------
        result = await execute_script(script, executor=executor)
        print(f"执行结果：ok={result.ok}  耗时={result.duration_ms}ms  "
              f"指标数={len(result.metrics)}")
        if result.error:
            print(f"错误：{result.error[:300]}")
        print()

        # ---------- 按类别汇总 ----------
        by_key: dict[str, list] = {}
        for m in result.metrics:
            by_key.setdefault(m.key, []).append(m)

        expected = [
            "cpu.usage_percent", "mem.used_percent", "mem.total_mb",
            "load.1m", "disk.used_percent", "disk.total_mb",
            "disk.inode_used_percent", "proc.count", "system.uptime_sec",
            "tcp.established", "net.rx_bytes", "disk.io_read_bytes",
        ]
        print("关键指标采集情况：")
        missing = []
        for key in expected:
            items = by_key.get(key)
            if not items:
                missing.append(key)
                print(f"   ✕ {key:<28} 未采集到")
            else:
                sample = items[0]
                label = f" {sample.labels}" if sample.labels else ""
                extra = f"（共 {len(items)} 个维度）" if len(items) > 1 else ""
                print(f"   ✓ {key:<28} = {sample.value}{label}{extra}")

        print()
        print("磁盘明细（验证中文环境下的挂载点解析）：")
        for m in by_key.get("disk.used_percent", []):
            print(f"   {m.labels.get('mount', '?'):<24} {m.value}%")

        print()
        print("=" * 68)
        if missing:
            print(f"缺失 {len(missing)} 个指标：{missing}")
            print("（部分指标在容器或裁剪系统中本就不存在，不一定是缺陷）")
        if result.ok and len(result.metrics) >= 10:
            print("✓ 内置脚本在该环境工作正常")
            return 0
        print("✕ 采集结果不符合预期")
        return 1
    finally:
        await pool.close_all()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
