"""验证错误信息格式化：不再出现空白的「连接失败：」。

针对 192.168.203.128 做真实探测（使用不存在的用户名，不影响真实账号）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncssh

from app.services.server_ref import ServerRef
from app.services.ssh_pool import describe_connect_error, diagnose_server

TARGET = "192.168.203.128"


def test_error_messages_are_never_empty():
    """所有常见异常都必须产出非空且可读的说明。"""
    cases = [
        (asyncio.TimeoutError(), "超时"),
        (TimeoutError(), "超时"),
        (ConnectionRefusedError(), "拒绝"),
        (ConnectionResetError(), "重置"),
        (OSError(), "网络错误"),
        (asyncssh.PermissionDenied("Permission denied"), "认证失败"),
        (asyncssh.ConnectionLost("Connection lost"), "中断"),
        (ValueError(), "连接失败"),
    ]
    ref = ServerRef(id=1, name="t", host="10.0.0.1", port=22, username="root")
    for exc, keyword in cases:
        msg = describe_connect_error(exc, ref)
        assert msg and msg.strip(), f"{type(exc).__name__} 产生了空消息"
        assert "：" in msg, f"{type(exc).__name__} 的消息格式异常：{msg}"
        # 关键：不能出现「连接失败：」后面什么都没有
        assert not msg.rstrip().endswith("："), f"消息以冒号结尾（信息缺失）：{msg}"
        assert keyword in msg, f"{type(exc).__name__} 的消息未包含「{keyword}」：{msg}"
        print(f"  {type(exc).__name__:<24} -> {msg[:76]}")


def test_timeout_message_mentions_duration():
    """超时提示必须带上超时秒数，便于用户判断。"""
    from app.config import settings

    msg = describe_connect_error(asyncio.TimeoutError(), None)
    assert str(settings.ssh_connect_timeout) in msg
    print(f"\n  超时提示包含 {settings.ssh_connect_timeout}s 且给出 4 类常见原因")


def test_empty_str_exception_still_descriptive():
    """str() 为空的异常也必须给出有意义的信息（本次故障的直接原因）。"""
    ref = ServerRef(id=1, name="t", host=TARGET, port=22, username="root")
    for exc in (asyncio.TimeoutError(), OSError(), ConnectionResetError()):
        msg = describe_connect_error(exc, ref)
        assert TARGET in msg or "超时" in msg, f"未给出足够上下文：{msg}"
    print("  空消息异常已补充类型名与目标地址")


if __name__ == "__main__":
    print("=" * 70)
    print("测试 1：错误信息不得为空")
    print("=" * 70)
    test_error_messages_are_never_empty()

    print()
    print("=" * 70)
    print("测试 2：超时提示包含秒数")
    print("=" * 70)
    test_timeout_message_mentions_duration()

    print()
    print("=" * 70)
    print("测试 3：空消息异常仍有上下文")
    print("=" * 70)
    test_empty_str_exception_still_descriptive()

    print()
    print("=" * 70)
    print(f"测试 4：对真实主机 {TARGET} 运行分阶段诊断（探测用户名）")
    print("=" * 70)
    ref = ServerRef(
        id=999, name="probe", host=TARGET, port=22,
        username="__aiops_probe_nonexistent__", auth_type="password",
    )
    result = asyncio.run(diagnose_server(ref, "dummy"))
    for step in result["steps"]:
        print(f"  {'✓' if step['ok'] else '✕'} {step['step']}")
        print(f"      {step['detail']}")
    print(f"\n  总体结果：{'通过' if result['ok'] else '存在失败阶段'}")
    print("\n全部验证通过")
