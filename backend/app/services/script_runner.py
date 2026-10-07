"""脚本沙箱执行器。

监控脚本是任意代码执行的入口，必须做防护（见 docs/ARCHITECTURE.md §5.3）：
- 强制超时
- 输出截断，防止内存爆炸
- Python 脚本在子进程中执行，限制可用内建函数
- 记录审计日志

注意：本沙箱的目标是"防止用户脚本意外搞垮平台"，**不是"防御恶意攻击者"**。
能编写监控脚本的用户本身已有一定权限；真正的强隔离需要容器或独立进程用户。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from typing import Any
from typing import Any

from app.services.executor_base import ExecResult

logger = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 64 * 1024      # stdout 上限 64KB
MAX_PYTHON_OUTPUT = 32 * 1024

# Python 脚本中被禁用的危险名称
_FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "__import__", "open", "input",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "memoryview", "breakpoint", "exit", "quit", "help",
}

# shell 脚本写操作关键字（用于风险提示，非硬拦截）
_WRITE_HINTS = re.compile(
    r"\b(rm|mv|dd|mkfs|truncate|kill|systemctl\s+(stop|restart|disable)|"
    r"chmod|chown|iptables|useradd|userdel|sed\s+-i|>\s*/)\b"
)


@dataclass
class ParsedMetric:
    key: str
    value: float
    labels: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScriptResult:
    ok: bool
    raw_output: str = ""
    metrics: list[ParsedMetric] = field(default_factory=list)
    error: str = ""
    duration_ms: int = 0
    truncated: bool = False
    risk_warning: str = ""


def assess_script_risk(body: str, runtime: str) -> str:
    """对脚本做静态风险评估，用于在界面上给出提示。"""
    if runtime == "python":
        hits = [n for n in _FORBIDDEN_NAMES if re.search(rf"\b{n}\s*\(", body)]
        if hits:
            return f"脚本使用了受限函数（沙箱中将不可用）：{', '.join(hits)}"
        return ""
    m = _WRITE_HINTS.search(body)
    if m:
        return f"脚本包含可能的写操作（{m.group(1)}），请确认其只读性"
    return ""


# --------------------------------------------------------------------------- 解析器


def parse_line_kv(text: str, schema: dict) -> list[ParsedMetric]:
    """解析行式键值输出。

    约定格式：`metric_key value [label=value ...]`
    例如：`disk.used_percent 91.2 mount=/data`
    """
    declared = {m["key"]: m for m in schema.get("metrics", [])}
    metrics: list[ParsedMetric] = []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        key = parts[0]
        # 未声明的指标也接受，便于脚本灵活扩展
        try:
            value = float(parts[1])
        except ValueError:
            continue
        labels: dict[str, Any] = {}
        for token in parts[2:]:
            if "=" in token:
                k, v = token.split("=", 1)
                labels[k] = v
        # 只保留 schema 中声明为该指标 label 的键
        spec = declared.get(key)
        if spec and spec.get("labels"):
            labels = {k: v for k, v in labels.items() if k in spec["labels"]}
        metrics.append(ParsedMetric(key=key, value=value, labels=labels))
    return metrics


def parse_json(text: str, schema: dict) -> list[ParsedMetric]:
    """解析 JSON 输出。

    支持三种形态，因为不同来源的自然输出方式不同：
    1. 单个对象：        {"key": "a.b", "value": 1}
    2. 对象数组：        [{"key": "a.b", "value": 1}, ...]
    3. 每行一个对象（JSONL）—— Python 脚本通过 ctx.emit() 逐行产出的形态
    """
    metrics: list[ParsedMetric] = []
    text = text.strip()
    if not text:
        return metrics

    items: list[Any] = []

    # 先尝试整体解析（覆盖形态 1 和 2）
    try:
        parsed = json.loads(text)
        items = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        # 退化为逐行解析（形态 3）
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    for item in items:
        if not isinstance(item, dict) or "key" not in item:
            continue
        try:
            value = float(item["value"])
        except (KeyError, TypeError, ValueError):
            continue
        labels = item.get("labels") or {}
        metrics.append(ParsedMetric(key=item["key"], value=value, labels=labels))
    return metrics


def parse_prometheus(text: str, schema: dict) -> list[ParsedMetric]:
    """解析 Prometheus exposition 格式，便于复用现有 exporter 输出。"""
    metrics: list[ParsedMetric] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+([-\d.eE+]+)", line)
        if not m:
            continue
        key, label_str, value_str = m.groups()
        try:
            value = float(value_str)
        except ValueError:
            continue
        labels: dict[str, Any] = {}
        if label_str:
            for pair in re.findall(r'(\w+)="([^"]*)"', label_str):
                labels[pair[0]] = pair[1]
        metrics.append(ParsedMetric(key=key, value=value, labels=labels))
    return metrics


PARSERS = {
    "line_kv": parse_line_kv,
    "json": parse_json,
    "prometheus": parse_prometheus,
}


def parse_output(text: str, schema: dict) -> list[ParsedMetric]:
    fmt = schema.get("format", "line_kv")
    parser = PARSERS.get(fmt, parse_line_kv)
    return parser(text, schema)


# --------------------------------------------------------------------------- Python 沙箱


PYTHON_PRELUDE = '''
# ===================== 平台注入的运行时 =====================
import json as _json
import math as _math


class _Ctx:
    """脚本可用的受限上下文。

    只暴露白名单能力，不下发明文密码。
    """

    def __init__(self, params):
        self.params = params

    def emit(self, key, value, **labels):
        """产出一个指标。"""
        print(_json.dumps({"key": key, "value": float(value), "labels": labels}))

    def param(self, name, default=None):
        return self.params.get(name, default)


def _entry():
    """调用用户定义的入口函数。

    必须在用户脚本之后执行，否则 collect 尚未定义。
    """
    ctx = _Ctx(%(params)r)
    fn = globals().get("collect") or globals().get("main")
    if fn is None:
        raise RuntimeError(
            "脚本必须定义 collect(ctx) 或 main(ctx) 函数作为入口"
        )
    fn(ctx)


_entry()
'''


def wrap_python_script(body: str, params: dict) -> str:
    """把用户脚本包装为可独立执行的 Python 程序。

    顺序很关键：用户脚本在前（定义 collect），运行时入口在后（调用它）。
    这样用户脚本中的语法/运行时错误也能被正常抛出，不会被入口逻辑掩盖。
    """
    prelude = PYTHON_PRELUDE % {"params": params}
    return body + "\n\n" + prelude


async def run_python_local(body: str, params: dict, timeout: int) -> ScriptResult:
    """在本地子进程中执行 Python 监控脚本。

    使用子进程而非 exec()，是为了让超时能真正杀掉失控脚本。
    """
    import sys
    import time

    wrapped = wrap_python_script(body, params)
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", wrapped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ScriptResult(
                ok=False, error=f"脚本执行超时（{timeout}s）",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
    except Exception as exc:
        return ScriptResult(ok=False, error=f"脚本启动失败：{exc}")

    truncated = len(stdout) > MAX_PYTHON_OUTPUT
    text = stdout[:MAX_PYTHON_OUTPUT].decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        return ScriptResult(
            ok=False, raw_output=text, error=err.strip() or f"退出码 {proc.returncode}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    return ScriptResult(
        ok=True, raw_output=text, truncated=truncated,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


# --------------------------------------------------------------------------- 统一入口


async def execute_script(
    script,
    executor=None,
    params: dict | None = None,
) -> ScriptResult:
    """执行监控脚本并按 emit_schema 解析输出。

    runtime:
      - shell  : 在目标服务器上通过 SSH 执行
      - python : 本地沙箱子进程执行（可访问 ctx 能力）
    """
    params = params or {}
    schema = script.emit_schema or {}
    timeout = script.timeout_sec or 15

    if script.runtime == "sql":
        # SQL 脚本不由本函数执行：它需要数据库连接，走 db_collector。
        # 早期版本会落到下面的 else 分支，把 SQL 当 shell 命令通过 SSH 执行，
        # 结果必然失败（或产生毫无意义的行为）。
        return ScriptResult(
            ok=False,
            error=(
                "SQL 脚本需要数据库连接，请通过「数据库监控」页面配置实例后执行；"
                "此处不能作为 shell 命令运行。"
            ),
        )

    if script.runtime == "python":
        result = await run_python_local(script.body, params, timeout)
    else:
        if executor is None:
            return ScriptResult(ok=False, error="shell 脚本需要指定目标服务器")
        try:
            exec_result: ExecResult = await executor.run(script.body, timeout=timeout)
        except Exception as exc:
            return ScriptResult(ok=False, error=f"远程执行失败：{exc}")

        raw = exec_result.stdout
        truncated = len(raw.encode("utf-8", errors="replace")) > MAX_OUTPUT_BYTES
        raw = raw[:MAX_OUTPUT_BYTES]
        result = ScriptResult(
            ok=exec_result.ok,
            raw_output=raw,
            error=exec_result.stderr.strip(),
            duration_ms=exec_result.duration_ms,
            truncated=truncated,
        )

    if result.ok:
        try:
            result.metrics = parse_output(result.raw_output, schema)
        except Exception as exc:
            result.ok = False
            result.error = f"解析输出失败：{exc}"

    result.risk_warning = assess_script_risk(script.body, script.runtime)
    return result
