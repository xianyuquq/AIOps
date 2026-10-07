"""测试所依赖的外部环境凭据。

**为什么要有这个文件**：真实口令一旦写进源码就会被提交到版本库，
推送后等同于公开。此前真实金仓口令就硬编码在几个验证脚本里，
是在准备建 git 仓库、做提交前密钥扫描时发现的。

约定：
  - **真实环境的凭据一律不给默认值**，必须通过环境变量传入；
    没传时脚本会明确告知并退出，而不是静默用某个占位符去连。
  - 本地 Docker 一次性容器允许有默认值：容器是临时创建的、
    口令也只在本机有效，不属于「真实环境凭据」。
    若你的本地容器用了别的口令，用 AIOPS_TEST_LOCAL_DB_PASSWORD 覆盖即可。

环境变量：
    AIOPS_TEST_KINGBASE_HOST     默认 192.168.203.128
    AIOPS_TEST_KINGBASE_PORT     默认 54321
    AIOPS_TEST_KINGBASE_USER     默认 system
    AIOPS_TEST_KINGBASE_DB       默认 test
    AIOPS_TEST_KINGBASE_PASSWORD 无默认，必填
    AIOPS_TEST_LOCAL_DB_PASSWORD 本地 Docker 库口令，默认见下
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------- Kingbase（真实环境）
KINGBASE_HOST = os.environ.get("AIOPS_TEST_KINGBASE_HOST", "192.168.203.128")
KINGBASE_PORT = int(os.environ.get("AIOPS_TEST_KINGBASE_PORT", "54321"))
KINGBASE_USER = os.environ.get("AIOPS_TEST_KINGBASE_USER", "system")
KINGBASE_DB = os.environ.get("AIOPS_TEST_KINGBASE_DB", "test")
KINGBASE_PASSWORD = os.environ.get("AIOPS_TEST_KINGBASE_PASSWORD", "")

# ---------------------------------------------------------------- 本地 Docker（一次性）
LOCAL_DB_HOST = os.environ.get("AIOPS_TEST_LOCAL_DB_HOST", "127.0.0.1")
LOCAL_DB_PASSWORD = os.environ.get("AIOPS_TEST_LOCAL_DB_PASSWORD", "Test1234")
LOCAL_PG_PORT = int(os.environ.get("AIOPS_TEST_LOCAL_PG_PORT", "5432"))
LOCAL_MYSQL_PORT = int(os.environ.get("AIOPS_TEST_LOCAL_MYSQL_PORT", "3306"))


def require_kingbase_password(script: str) -> str:
    """取金仓口令；未设置则给出可操作的提示并退出。"""
    if KINGBASE_PASSWORD:
        return KINGBASE_PASSWORD
    print(
        f"{script}：需要金仓口令。请先设置环境变量再运行，例如：\n"
        f'    $env:AIOPS_TEST_KINGBASE_PASSWORD = "你的口令"\n'
        f"（口令不写进源码，避免随版本库外泄）",
        file=sys.stderr,
    )
    raise SystemExit(2)
