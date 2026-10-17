"""天翼云盘 Provider 单元测试（mock cloud189，不依赖网络）。"""

from __future__ import annotations

import json

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Account
from panbridge.providers.cloud189.provider import Cloud189Provider


class _FakeCloud189:
    def __init__(self, config):
        self.config = config

    def get_disk_space_info(self):
        return {"res_code": 0}

    def get_all_files(self, folder_id):
        return [
            {"id": "1", "name": "a.txt", "size": 10, "is_folder": False},
            {"id": "2", "name": "dir", "is_folder": True},
        ]

    def download(self, file_id):
        return "https://download.189.cn/x"


class _FakeCipher:
    def decrypt(self, token):
        return json.dumps({"username": "u", "password": "p"})


def _account() -> Account:
    return Account(
        id="a1",
        provider="cloud189",
        label="test",
        encrypted_credential="enc",
    )


@pytest.mark.asyncio
async def test_list_account_files(monkeypatch):
    import panbridge.providers.cloud189.provider as mod

    mod.Cloud189 = _FakeCloud189
    p = Cloud189Provider(cipher=_FakeCipher())
    files = await p.list_account_files(_account())
    assert len(files) == 2
    assert files[0].name == "a.txt"
    assert files[0].is_dir is False
    assert files[1].name == "dir"
    assert files[1].is_dir is True


@pytest.mark.asyncio
async def test_get_download_info(monkeypatch):
    import panbridge.providers.cloud189.provider as mod

    mod.Cloud189 = _FakeCloud189
    p = Cloud189Provider(cipher=_FakeCipher())
    info = await p.get_download_info(_account(), "1")
    assert info["url"] == "https://download.189.cn/x"


@pytest.mark.asyncio
async def test_validate_account(monkeypatch):
    import panbridge.providers.cloud189.provider as mod

    mod.Cloud189 = _FakeCloud189
    p = Cloud189Provider(cipher=_FakeCipher())
    assert await p.validate_account(_account()) is True


@pytest.mark.asyncio
async def test_inspect_share_unsupported(monkeypatch):
    import panbridge.providers.cloud189.provider as mod

    mod.Cloud189 = _FakeCloud189
    p = Cloud189Provider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("abc", None)
    assert exc.value.code == ErrorCode.UNSUPPORTED_OPERATION


@pytest.mark.asyncio
async def test_save_shared_files_unsupported(monkeypatch):
    import panbridge.providers.cloud189.provider as mod

    mod.Cloud189 = _FakeCloud189
    p = Cloud189Provider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.save_shared_files(_account(), "x", None, [], None, "skip")
    assert exc.value.code == ErrorCode.UNSUPPORTED_OPERATION
