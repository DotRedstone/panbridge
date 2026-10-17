"""凭据加密存储。

使用 Fernet（对称加密），主密钥通过外部 secret 注入（`PANBRIDGE_SECRET_KEY`）。
未配置主密钥时抛出 ConfigError，绝不静默使用不安全的默认密钥。
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet


class CredentialCipher:
    def __init__(self, secret_key: str) -> None:
        if not secret_key:
            raise ValueError(
                "PANBRIDGE_SECRET_KEY 未配置，无法安全加密/解密凭据。"
                "请通过环境变量注入强随机密钥。"
            )
        self._fernet = Fernet(self._derive_key(secret_key))

    @staticmethod
    def _derive_key(secret_key: str) -> bytes:
        # Fernet 需要 32 字节 urlsafe base64 编码的密钥。
        # 用 SHA-256 派生，使任意长度的 secret 都能作为主密钥。
        digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
