"""远程执行器抽象。

设计动机：需要同时支持 Linux（SSH）与 Windows Server（WinRM），
若把 SSH 细节散落在业务代码中，后续接入 WinRM 将需要大量改动。
因此所有远程操作统一走 Executor 接口。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable


@dataclass
class ExecResult:
    """非交互式命令执行结果。"""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int = 0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class FileEntry:
    """远端文件/目录条目。"""

    name: str
    path: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    mode: str = ""


@dataclass(eq=False)
class ChannelHandle:
    """一个交互式终端通道。

    注意：必须使用 eq=False（身份语义）而非默认的字段相等语义。
    SSHExecutor 会把 handle 放进 set 以便统一关闭，而默认 dataclass
    是不可哈希的，会导致每次建立真实 SSH 终端都抛
    "unhashable type: 'ChannelHandle'"。
    """

    server_id: int
    server_name: str
    write: Callable[[str], Awaitable[None]]
    resize: Callable[[int, int], Awaitable[None]]
    close: Callable[[], Awaitable[None]]
    meta: dict[str, Any] = field(default_factory=dict)


ProgressCallback = Callable[[int, int], Awaitable[None]]
# 回调签名：(已传输字节, 总字节)


@runtime_checkable
class OutputSink(Protocol):
    """终端输出的投递目标（由 WebSocket 层实现）。"""

    async def push_output(self, server_id: int, data: str) -> None: ...
    async def push_status(self, server_id: int, status: str, message: str = "") -> None: ...


class Executor(abc.ABC):
    """远程执行器接口。

    实现：
    - SSHExecutor  : Linux / 麒麟 / 统信（asyncssh）
    - WinRMExecutor: Windows Server（pywinrm）—— P7 阶段实现
    """

    def __init__(self, server_id: int, name: str) -> None:
        self.server_id = server_id
        self.name = name
        self.is_connected = False

    @abc.abstractmethod
    async def connect(self) -> None:
        """建立连接。失败时抛异常。"""

    @abc.abstractmethod
    async def close(self) -> None:
        """关闭连接并释放资源。"""

    @abc.abstractmethod
    async def run(self, command: str, timeout: int | None = None) -> ExecResult:
        """执行一次性命令，返回完整输出。"""

    @abc.abstractmethod
    async def open_terminal(
        self,
        sink: OutputSink,
        cols: int = 120,
        rows: int = 30,
        echo_off: bool = False,
    ) -> ChannelHandle:
        """打开交互式终端通道。

        echo_off=True 时关闭远端回显——广播模式下这是必需的，
        否则四个终端会出现重复显示（见 ARCHITECTURE.md §6.3）。
        """

    @abc.abstractmethod
    async def upload(self, local_path: str, remote_path: str,
                     on_progress: ProgressCallback | None = None) -> None:
        ...

    @abc.abstractmethod
    async def download(self, remote_path: str, local_path: str,
                       on_progress: ProgressCallback | None = None) -> None:
        ...

    @abc.abstractmethod
    async def list_dir(self, remote_path: str) -> list[FileEntry]:
        ...
