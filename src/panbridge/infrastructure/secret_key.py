"""主密钥（secret key）的自动生成与持久化。

本地单用户场景下，用户不希望手工生成并填写加密主密钥。
本模块在首次运行时自动生成强随机密钥并保存到用户配置目录，
文件权限设为仅当前用户可读写（0600），且不进入 Git。

优先级：
1. 环境变量 `PANBRIDGE_SECRET_KEY`（显式注入，仍优先）。
2. 本地密钥文件（自动生成，路径由 `PANBRIDGE_SECRET_KEY_FILE` 覆盖）。

密钥文件绝不被提交到 Git（由 .gitignore 统一排除）。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def _default_key_file() -> Path:
    """默认密钥文件路径：~/.config/panbridge/secret.key。"""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg)
    else:
        base = Path.home() / ".config"
    return base / "panbridge" / "secret.key"


def load_or_create_secret_key(
    env_override: str | None = None,
    key_file: str | None = None,
) -> str:
    """返回可用的主密钥，必要时自动生成并持久化。

    顺序：环境变量 > 已存在的密钥文件 > 生成新密钥并保存。

    密钥以 urlsafe base64 形式生成（`secrets.token_urlsafe(32)`），
    与 `CredentialCipher` 的任意长度密钥派生兼容。
    """
    # 1. 环境变量显式注入优先（生产/多进程共享时使用）
    env_value = env_override if env_override is not None else os.environ.get(
        "PANBRIDGE_SECRET_KEY", ""
    )
    if env_value:
        return env_value

    # 2. 读取本地密钥文件
    path = Path(key_file) if key_file else _default_key_file()
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value

    # 3. 生成新密钥并持久化（首次初始化）
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    # 先写入再收紧权限，避免竞态窗口
    path.write_text(value + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - 部分平台 chmod 不可用
        pass
    return value
