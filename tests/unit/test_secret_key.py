"""主密钥自动生成与持久化单元测试。"""

from __future__ import annotations

from panbridge.infrastructure.secret_key import load_or_create_secret_key


def test_env_override_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("PANBRIDGE_SECRET_KEY", "env-secret")
    key_file = tmp_path / "secret.key"
    assert load_or_create_secret_key(key_file=str(key_file)) == "env-secret"
    assert not key_file.exists()


def test_creates_key_file_on_first_run(tmp_path, monkeypatch):
    monkeypatch.delenv("PANBRIDGE_SECRET_KEY", raising=False)
    key_file = tmp_path / "secret.key"
    value = load_or_create_secret_key(key_file=str(key_file))
    assert value
    # 再次调用应返回相同密钥（幂等）
    assert load_or_create_secret_key(key_file=str(key_file)) == value


def test_returns_existing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("PANBRIDGE_SECRET_KEY", raising=False)
    key_file = tmp_path / "secret.key"
    key_file.write_text("pre-existing-key\n", encoding="utf-8")
    assert (
        load_or_create_secret_key(key_file=str(key_file))
        == "pre-existing-key"
    )


def test_key_file_permissions_0600(tmp_path, monkeypatch):
    monkeypatch.delenv("PANBRIDGE_SECRET_KEY", raising=False)
    key_file = tmp_path / "secret.key"
    load_or_create_secret_key(key_file=str(key_file))
    mode = key_file.stat().st_mode & 0o777
    assert mode == 0o600
