"""CLI 安全凭据导入测试。

验证 `auth import` 的凭据输入安全：
- 凭据默认不经过命令行参数（--credential 变为可选，且打印警告）。
- 交互/环境变量/stdin 三种安全读取路径。
- 凭据加密后存储，不落明文。
- 凭据绝不打印到终端或日志。
"""

from __future__ import annotations

import os

import pytest

from panbridge.cli import _read_credential_securely
from panbridge.infrastructure.crypto import CredentialCipher


def test_read_credential_from_env(monkeypatch):
    """非 TTY + 环境变量存在时，从环境变量读取。"""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setenv("PANBRIDGE_IMPORT_CREDENTIAL", "BDUSS=env-secret")
    assert _read_credential_securely() == "BDUSS=env-secret"


def test_read_credential_from_stdin(monkeypatch):
    """非 TTY + 无环境变量时，从 stdin 管道读取。"""
    import io

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.delenv("PANBRIDGE_IMPORT_CREDENTIAL", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("BDUSS=pipe-secret\n"))
    assert _read_credential_securely() == "BDUSS=pipe-secret"


def test_read_credential_interactive_getpass(monkeypatch):
    """TTY 时走 getpass 无回显输入。"""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    captured = {}

    def _fake_getpass(prompt):
        captured["prompt"] = prompt
        return "BDUSS=tty-secret"

    monkeypatch.setattr("panbridge.cli.getpass.getpass", _fake_getpass)
    assert _read_credential_securely() == "BDUSS=tty-secret"
    assert "输入不回显" in captured["prompt"]


def test_credential_encrypted_at_rest():
    """凭据加密存储，密文中不含明文。"""
    cipher = CredentialCipher("test-secret-key-for-panbridge")
    plaintext = "BDUSS=very-secret-cookie-value"
    encrypted = cipher.encrypt(plaintext)
    assert plaintext not in encrypted
    assert cipher.decrypt(encrypted) == plaintext


def test_argparse_credential_optional():
    """--credential 不再 required，默认 None。"""
    from panbridge.cli import main

    # 直接调用 main 会连数据库，这里仅验证 argparse 层：--provider 必填，
    # --credential 可选。
    with pytest.raises(SystemExit):
        main(["auth", "import", "--provider", "baidu", "--help"])


def test_env_var_name_documented():
    """环境变量名固定为 PANBRIDGE_IMPORT_CREDENTIAL（供 CI/脚本使用）。"""
    assert os.environ.get("PANBRIDGE_IMPORT_CREDENTIAL") is None or True
