"""集中管理凭据加密、管理员密码哈希与 Web 访问令牌。

主密钥是数据库中加密凭据的恢复根，调用方不得记录解密后的明文。
"""

from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from devops.domain.models import utcnow


class SecretManager:
    def __init__(self, key: bytes) -> None:
        """使用主密钥初始化凭据加密器和 JWT 签名派生密钥。"""
        self._fernet = Fernet(key)
        self.signing_key = hashlib.sha256(key).hexdigest()

    @classmethod
    def from_key_file(cls, path: Path) -> SecretManager:
        """原子创建或读取主密钥文件，保证并发启动不会产生分叉密钥。"""
        resolved = path.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if resolved.exists():
            key = resolved.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            # O_EXCL 防止并发启动各自生成不同主密钥；0600 避免其他本机用户读取。
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            descriptor = os.open(resolved, flags, 0o600)
            try:
                os.write(descriptor, key + b"\n")
            finally:
                os.close(descriptor)
        return cls(key)

    def encrypt(self, value: str) -> bytes:
        """加密凭据明文；调用方不得把返回前的明文写入日志或数据库。"""
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, value: bytes) -> str:
        """解密凭据，主密钥不匹配时转换为不泄露明文的业务错误。"""
        try:
            return self._fernet.decrypt(value).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("credential cannot be decrypted with the configured master key") from exc


password_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
DUMMY_PASSWORD_HASH = password_hasher.hash("invalid-account-timing-placeholder")


def hash_password(password: str) -> str:
    """使用 Argon2id 生成管理员密码哈希，不保存可逆密码。"""
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """验证密码哈希，并将格式错误和不匹配统一为 False。"""
    try:
        return password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def create_access_token(
    *,
    subject: str,
    username: str,
    signing_key: str,
    issuer: str,
    expires_minutes: int,
) -> tuple[str, int]:
    """签发带发行方和有效期的短期 JWT，并返回剩余秒数。"""
    now = utcnow()
    expires = now + timedelta(minutes=expires_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "username": username,
        "iss": issuer,
        "iat": now,
        "nbf": now,
        "exp": expires,
    }
    return jwt.encode(payload, signing_key, algorithm="HS256"), int((expires - now).total_seconds())


def decode_access_token(token: str, *, signing_key: str, issuer: str) -> dict[str, Any]:
    """校验 JWT 签名、发行方和时间窗口，失败由调用方转换为未授权响应。"""
    return jwt.decode(token, signing_key, algorithms=["HS256"], issuer=issuer)
