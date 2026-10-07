"""服务器信息快照。

问题背景：SQLAlchemy 的 ORM 对象绑定在 Session 上。一旦 Session 关闭
（或 commit 后属性过期），再访问 `server.name` 就会抛 DetachedInstanceError。
而 SSH 执行器需要长期持有服务器信息（连接池跨请求复用），终端会话也会在
数据库会话关闭后继续使用服务器名称。

因此凡是需要跨 Session 生命周期使用的场景，一律先转成 ServerRef 快照。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServerRef:
    """与数据库会话无关的服务器信息快照。"""

    id: int
    name: str
    host: str
    port: int
    username: str
    auth_type: str = "password"
    protocol: str = "ssh"
    secret_ciphertext: bytes | None = None
    secret_nonce: bytes | None = None

    @property
    def platform(self) -> str:
        """推断目标平台，用于选择采集脚本。"""
        return "windows" if self.protocol == "winrm" else "linux"


def to_ref(server) -> ServerRef:
    """把 ORM Server 对象（或已有快照）转成 ServerRef。

    必须在 Session 仍然打开时调用。
    """
    if isinstance(server, ServerRef):
        return server
    return ServerRef(
        id=server.id,
        name=server.name,
        host=server.host,
        port=server.port,
        username=server.username,
        auth_type=getattr(server, "auth_type", "password") or "password",
        protocol=getattr(server, "protocol", "ssh") or "ssh",
        secret_ciphertext=server.secret_ciphertext,
        secret_nonce=server.secret_nonce,
    )
