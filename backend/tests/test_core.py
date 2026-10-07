"""核心逻辑测试。

运行：python -m pytest tests/ -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.alert_engine import _condition_met, make_fingerprint
from app.services.script_runner import (
    assess_script_risk,
    parse_output,
    run_python_local,
)
from app.services.terminal import is_dangerous


# ============================================================ 脚本解析


def test_parse_line_kv_basic():
    schema = {"format": "line_kv", "metrics": [{"key": "disk.used_percent"}]}
    text = "disk.used_percent 91.2\ndisk.used_percent 55\n"
    metrics = parse_output(text, schema)
    assert len(metrics) == 2
    assert metrics[0].key == "disk.used_percent"
    assert metrics[0].value == 91.2


def test_parse_line_kv_with_labels():
    schema = {
        "format": "line_kv",
        "metrics": [{"key": "disk.used_percent", "labels": ["mount"]}],
    }
    metrics = parse_output("disk.used_percent 91.2 mount=/data", schema)
    assert len(metrics) == 1
    assert metrics[0].labels == {"mount": "/data"}


def test_parse_line_kv_ignores_comments_and_bad_lines():
    schema = {"format": "line_kv", "metrics": []}
    text = "# comment\n\ndisk.used_percent 91.2\nonly_one_field\nbad value_here\n"
    metrics = parse_output(text, schema)
    assert len(metrics) == 1
    assert metrics[0].value == 91.2


def test_parse_line_kv_filters_undeclared_labels():
    """只保留 schema 中声明的 label，避免把噪音写进数据库。"""
    schema = {
        "format": "line_kv",
        "metrics": [{"key": "disk.used_percent", "labels": ["mount"]}],
    }
    metrics = parse_output("disk.used_percent 91.2 mount=/data extra=noise", schema)
    assert metrics[0].labels == {"mount": "/data"}


def test_parse_json_format():
    schema = {"format": "json"}
    text = '[{"key": "app.heap_mb", "value": "512", "labels": {"jvm": "main"}}]'
    metrics = parse_output(text, schema)
    assert len(metrics) == 1
    assert metrics[0].key == "app.heap_mb"
    assert metrics[0].value == 512.0
    assert metrics[0].labels == {"jvm": "main"}


def test_parse_json_invalid_returns_empty():
    assert parse_output("not json", {"format": "json"}) == []


def test_parse_prometheus_format():
    schema = {"format": "prometheus"}
    text = '# HELP x\ndisk_used_percent{mount="/data"} 91.2\n'
    metrics = parse_output(text, schema)
    assert len(metrics) == 1
    assert metrics[0].key == "disk_used_percent"
    assert metrics[0].labels == {"mount": "/data"}


def test_parse_real_linux_script_shape():
    """用真实 df/free 输出的形态验证解析。"""
    schema = {"format": "line_kv", "metrics": [{"key": "disk.used_percent"}]}
    real_output = (
        "cpu.usage_percent 12.34\n"
        "load.1m 0.52\n"
        "mem.used_percent 43.21\n"
        "disk.used_percent 91 mount=/data\n"
        "disk.used_percent 55 mount=/\n"
        "proc.count 231\n"
    )
    metrics = parse_output(real_output, schema)
    keys = [m.key for m in metrics]
    assert "cpu.usage_percent" in keys
    assert "mem.used_percent" in keys
    assert len(metrics) == 6


# ============================================================ Python 沙箱


def test_python_sandbox_happy_path():
    body = """
def collect(ctx):
    ctx.emit('app.heap_mb', 512)
    ctx.emit('app.queue', 3, queue='orders')
"""
    result = asyncio.run(run_python_local(body, {}, 10))
    assert result.ok, result.error
    metrics = parse_output(result.raw_output, {"format": "json"})
    assert {m.key for m in metrics} == {"app.heap_mb", "app.queue"}
    queue_metric = next(m for m in metrics if m.key == "app.queue")
    assert queue_metric.labels == {"queue": "orders"}


def test_python_sandbox_does_not_restrict_os_access():
    """记录沙箱的真实边界：它**不能**阻止脚本执行系统命令。

    子进程继承完整 OS 权限，因此 os.system 会成功。这不影响授权用户
    （他们本就有服务器登录权限），但意味着**不可信用户不能获得脚本编写权限**。
    强隔离需依赖容器或独立低权限进程用户，见 ARCHITECTURE.md §5.3。
    """
    body = """
def collect(ctx):
    import os
    os.system('echo pwned')
"""
    result = asyncio.run(run_python_local(body, {}, 10))
    # 当前实现的真实行为：命令确实执行了
    assert result.ok
    assert "pwned" in result.raw_output


def test_arbitrary_python_executes_and_can_be_parsed():
    """脚本能自由使用标准库（这是设计目标：给用户足够的采集能力）。"""
    body = """
import platform

def collect(ctx):
    ctx.emit('host.python_major', int(platform.python_version_tuple()[0]))
"""
    result = asyncio.run(run_python_local(body, {}, 10))
    assert result.ok, result.error
    metrics = parse_output(result.raw_output, {"format": "json"})
    assert metrics and metrics[0].key == "host.python_major"


def test_python_sandbox_enforces_timeout():
    """死循环脚本必须被超时杀掉，不能拖垮采集调度。"""
    body = """
def collect(ctx):
    while True:
        pass
"""
    result = asyncio.run(run_python_local(body, {}, 2))
    assert not result.ok
    assert "超时" in result.error


def test_python_sandbox_requires_collect_or_main():
    result = asyncio.run(run_python_local("x = 1", {}, 10))
    assert not result.ok


def test_python_sandbox_captures_error():
    body = """
def collect(ctx):
    raise ValueError('boom')
"""
    result = asyncio.run(run_python_local(body, {}, 10))
    assert not result.ok
    assert "boom" in result.error


# ============================================================ 风险评估


def test_risk_assessment_detects_write_ops():
    assert assess_script_risk("rm -rf /tmp/x", "shell")
    assert assess_script_risk("systemctl restart nginx", "shell")


def test_risk_assessment_clean_readonly_script():
    assert assess_script_risk("df -PT | awk '{print $6}'", "shell") == ""


def test_risk_assessment_flags_python_builtins():
    warning = assess_script_risk("eval('1')", "python")
    assert "受限函数" in warning


# ============================================================ 危险命令拦截


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -rf /*",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        ":(){ :|:& };:",
        "shutdown -h now",
        "reboot",
    ],
)
def test_dangerous_commands_blocked(command):
    assert is_dangerous(command), f"应拦截：{command}"


@pytest.mark.parametrize(
    "command",
    ["df -Th", "ls -la /var/log", "systemctl status nginx", "free -m", "top -bn1"],
)
def test_normal_commands_allowed(command):
    assert not is_dangerous(command), f"不应拦截：{command}"


# ============================================================ 告警逻辑


def test_condition_operators():
    assert _condition_met(">", 91, 90)
    assert not _condition_met(">", 90, 90)
    assert _condition_met(">=", 90, 90)
    assert _condition_met("<", 5, 10)
    assert _condition_met("==", 1, 1)
    assert _condition_met("!=", 2, 1)
    assert not _condition_met("??", 2, 1)


def test_fingerprint_aggregates_same_fault():
    """同一规则+指标+标签必须产生相同指纹，这是降噪聚合的基础。"""
    a = make_fingerprint(1, "disk.used_percent", {"mount": "/data"})
    b = make_fingerprint(1, "disk.used_percent", {"mount": "/data"})
    assert a == b


def test_fingerprint_differs_by_metric_and_labels():
    a = make_fingerprint(1, "disk.used_percent", {"mount": "/data"})
    b = make_fingerprint(1, "disk.used_percent", {"mount": "/"})
    c = make_fingerprint(1, "cpu.usage_percent", {"mount": "/data"})
    d = make_fingerprint(2, "disk.used_percent", {"mount": "/data"})
    assert len({a, b, c, d}) == 4


def test_fingerprint_label_order_independent():
    a = make_fingerprint(1, "m", {"x": "1", "y": "2"})
    b = make_fingerprint(1, "m", {"y": "2", "x": "1"})
    assert a == b


# ============================================================ 加密


def test_credential_encryption_roundtrip():
    from app.core.crypto import decrypt_secret, encrypt_secret

    secret = "p@ssw0rd-测试-🔐"
    ciphertext, nonce = encrypt_secret(secret)
    assert decrypt_secret(ciphertext, nonce) == secret


def test_credential_encryption_uses_unique_nonce():
    from app.core.crypto import encrypt_secret

    a, _ = encrypt_secret("same")
    b, _ = encrypt_secret("same")
    assert a != b, "相同明文必须产生不同密文（nonce 不可复用）"


def test_password_hashing():
    from app.core.crypto import hash_password, verify_password

    hashed = hash_password("secret123")
    assert verify_password("secret123", hashed)
    assert not verify_password("wrong", hashed)


def test_password_over_bcrypt_limit():
    """bcrypt 只处理 72 字节，超长密码必须被安全截断而非报错。"""
    from app.core.crypto import hash_password, verify_password

    long_password = "x" * 200
    hashed = hash_password(long_password)
    assert verify_password(long_password, hashed)
