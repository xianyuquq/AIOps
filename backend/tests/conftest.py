"""pytest 全局配置。

在**任何** app 模块被导入之前把配置指向临时数据库，
避免测试读写真实的 data/aiops.db（也避免覆盖真实的 master.key）。

pytest 会最先导入 conftest.py，因此这里的赋值早于测试模块的 import。

关于临时目录的两个坑（都已处理）：

1. **必须指定明确的父目录。** 只写 `mkdtemp(prefix=...)` 时，Python 会依次尝试
   TMPDIR/TEMP/TMP，都不可写时**回退到当前工作目录**。此前因此把一批
   `aiops_pytest_*` 目录散落在 backend/ 下（受限环境下 TEMP 不可写就会触发）。

2. **清理会静默失败。** Windows 下无法删除仍被占用的文件，
   而 `shutil.rmtree(..., ignore_errors=True)` 不会报错；
   SQLAlchemy 引擎若还持有 test.db，目录就会残留。
   因此这里改用**固定目录**：每次启动先清空再使用，
   于是无论上一次是崩溃、Ctrl+C 还是被 kill，都不会越积越多。

> 注意：固定目录意味着**同一时间只应跑一个 pytest 会话**。
> 对本项目（本地单机开发）这是合适的取舍；若要并发跑测试，
> 需要改回 mkdtemp 并另想办法回收。
"""

from __future__ import annotations

import atexit
import os
import shutil
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TMP_BASE = _REPO_ROOT / ".pytest-tmp"
_TEST_DIR = _TMP_BASE / "run"

# 每次启动先清空：上一次运行无论怎么结束，都不会留下垃圾
if _TEST_DIR.exists():
    shutil.rmtree(_TEST_DIR, ignore_errors=True)
_TEST_DIR.mkdir(parents=True, exist_ok=True)

# 顺带清掉历史版本散落在别处的残留（旧行为：mkdtemp 落在 CWD）
for _legacy_root in (_TMP_BASE, _REPO_ROOT / "backend"):
    for _stale in _legacy_root.glob("aiops_pytest_*"):
        shutil.rmtree(_stale, ignore_errors=True)

os.environ["AIOPS_DATABASE_URL"] = f"sqlite:///{(_TEST_DIR / 'test.db').as_posix()}"
os.environ["AIOPS_DATA_DIR"] = str(_TEST_DIR)
os.environ["AIOPS_SECRET_KEY"] = "pytest-secret-key-not-for-production"
os.environ["AIOPS_ADMIN_PASSWORD"] = "admin123"
os.environ["AIOPS_LLM_ENABLED"] = "false"
# 缩短超时，避免测试里连不上的服务器拖慢用例
os.environ["AIOPS_SSH_CONNECT_TIMEOUT"] = "3"


@atexit.register
def _cleanup() -> None:
    """先释放数据库连接再删目录，否则 Windows 上会因文件占用而删不掉。"""
    try:
        from app.db import engine

        engine.dispose()
    except Exception:
        pass
    shutil.rmtree(_TEST_DIR, ignore_errors=True)


def pytest_report_header(config) -> str:
    return f"测试数据目录：{_TEST_DIR}"

