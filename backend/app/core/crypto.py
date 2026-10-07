"""凭据加密与密码哈希。

设计要点：
- 服务器密码 / 私钥使用 AES-256-GCM 信封加密后落库，明文永不入库、永不出现在日志中。
- 主密钥优先来自环境变量 AIOPS_MASTER_KEY_B64；未配置时自动生成并保存到
  data/master.key（权限 0600），避免"密钥与密文分离"在单机部署下变成额外负担。
- 用户登录密码使用 bcrypt。
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

import bcrypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

_NONCE_SIZE = 12  # AES-GCM 推荐 96-bit nonce
_BCRYPT_MAX_BYTES = 72  # bcrypt 算法硬限制


# --------------------------------------------------------------------------- 主密钥


def _load_or_create_master_key() -> bytes:
    """加载主密钥；不存在则生成并持久化。"""
    if settings.master_key_b64:
        key = base64.b64decode(settings.master_key_b64)
        if len(key) != 32:
            raise ValueError("AIOPS_MASTER_KEY_B64 必须是 32 字节的 base64 编码")
        return key

    key_path = Path(settings.data_dir) / "master.key"
    if key_path.exists():
        raw = key_path.read_bytes().strip()
        key = base64.b64decode(raw)
        if len(key) != 32:
            raise ValueError(f"主密钥文件损坏（长度 {len(key)} != 32）：{key_path}")
        return key

    key = AESGCM.generate_key(bit_length=256)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(base64.b64encode(key))
    try:
        os.chmod(key_path, 0o600)  # Windows 上是 no-op，但语义正确
    except OSError:
        pass
    return key


_master_key: bytes | None = None


def get_master_key() -> bytes:
    global _master_key
    if _master_key is None:
        _master_key = _load_or_create_master_key()
    return _master_key


# --------------------------------------------------------------------------- 凭据加解密


def encrypt_secret(plaintext: str) -> tuple[bytes, bytes]:
    """加密敏感字符串。

    返回 (ciphertext, nonce)。同一明文每次加密结果不同（nonce 随机）。
    """
    aesgcm = AESGCM(get_master_key())
    nonce = secrets.token_bytes(_NONCE_SIZE)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return ciphertext, nonce


def decrypt_secret(ciphertext: bytes, nonce: bytes) -> str:
    """解密敏感字符串。密钥不匹配或数据被篡改时抛 InvalidTag。"""
    aesgcm = AESGCM(get_master_key())
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")


# --------------------------------------------------------------------------- 密码哈希


def _to_bcrypt_bytes(password: str) -> bytes:
    """bcrypt 只处理前 72 字节，超出会直接报错，因此显式截断。"""
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_to_bcrypt_bytes(password), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_to_bcrypt_bytes(plain), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False
