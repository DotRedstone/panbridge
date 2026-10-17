"""真实 QuarkProvider 单元测试。

通过 mock quark_client 验证字段映射与错误映射，
不访问真实网盘（遵守「自动化测试不得操作真实网盘」原则）。
"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Account
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.providers.quark.provider import QuarkProvider


@pytest.fixture
def quark():
    return QuarkProvider()


@pytest.fixture
def account():
    return Account(
        id="acc1",
        provider="quark",
        label="test",
        encrypted_credential="fake-cookie",
    )


@pytest.fixture
def cipher():
    return CredentialCipher("test-secret-key-for-panbridge")


def test_map_file(quark):
    raw = {
        "fid": "f123",
        "file_name": "video.mkv",
        "dir": False,
        "size": 2048,
        "pdir_fid": "0",
    }
    f = quark._map_file(raw)
    assert f.file_id == "f123"
    assert f.name == "video.mkv"
    assert f.is_dir is False
    assert f.size == 2048


def test_map_file_dir(quark):
    raw = {"fid": "d1", "file_name": "folder", "dir": True}
    f = quark._map_file(raw)
    assert f.is_dir is True


def test_extract_files(quark):
    info = {
        "data": {
            "list": [
                {"fid": "f1", "file_name": "a.mkv", "dir": False},
                {"fid": "d1", "file_name": "sub", "dir": True},
            ]
        }
    }
    files = quark._extract_files(info)
    assert len(files) == 2
    assert files[0].file_id == "f1"
    assert files[1].is_dir is True


def test_extract_files_skips_malformed(quark):
    info = {"data": {"list": [{"file_name": "no-fid"}]}}
    assert quark._extract_files(info) == []


def test_map_quark_auth_error(quark):
    from quark_client.exceptions import AuthenticationError

    exc = quark._map_quark_error(AuthenticationError("bad cookie"))
    assert exc.code == ErrorCode.ACCOUNT_EXPIRED


def test_map_quark_share_link_error(quark):
    from quark_client import ShareLinkError

    exc = quark._map_quark_error(ShareLinkError("bad link"))
    assert exc.code == ErrorCode.INVALID_SHARE


def test_map_quark_quota_error(quark):
    from quark_client.exceptions import APIError

    exc = quark._map_quark_error(APIError("空间不足"))
    assert exc.code == ErrorCode.QUOTA_EXCEEDED


def test_map_quark_not_found_error(quark):
    from quark_client.exceptions import APIError

    exc = quark._map_quark_error(APIError("HTTP错误: 404"))
    assert exc.code == ErrorCode.INVALID_SHARE


def test_map_quark_password_error(quark):
    from quark_client.exceptions import APIError

    exc = quark._map_quark_error(APIError("提取码错误"))
    assert exc.code == ErrorCode.INVALID_PASSWORD


def test_missing_credential_raises(quark):
    acct = Account(id="a", provider="quark", label="x", encrypted_credential="")
    with pytest.raises(DomainError) as exc:
        quark._client_for(acct)
    assert exc.value.code == ErrorCode.ACCOUNT_EXPIRED


def test_decrypt_credential_roundtrip(cipher):
    """加密后再解密应还原明文 Cookie。"""
    provider = QuarkProvider(cipher=cipher)
    plaintext = "cookie1=abc; cookie2=def"
    token = cipher.encrypt(plaintext)
    acct = Account(
        id="a", provider="quark", label="x", encrypted_credential=token
    )
    assert provider._decrypt_credential(acct) == plaintext


def test_decrypt_without_cipher_raises(quark):
    acct = Account(
        id="a", provider="quark", label="x", encrypted_credential="some-token"
    )
    with pytest.raises(DomainError) as exc:
        quark._decrypt_credential(acct)
    assert exc.value.code == ErrorCode.INTERNAL_ERROR


def test_decrypt_with_wrong_key_raises(cipher):
    """用错误的密钥解密应映射为 ACCOUNT_EXPIRED。"""
    provider = QuarkProvider(cipher=cipher)
    other = CredentialCipher("a-different-secret-key")
    token = other.encrypt("cookie")
    acct = Account(
        id="a", provider="quark", label="x", encrypted_credential=token
    )
    with pytest.raises(DomainError) as exc:
        provider._decrypt_credential(acct)
    assert exc.value.code == ErrorCode.ACCOUNT_EXPIRED


def test_cipher_rejects_empty_secret():
    with pytest.raises(ValueError):
        CredentialCipher("")


class _FakeQuarkClient:
    """mock QuarkClient：验证 resolve/get_download_info 的解析逻辑。"""

    def __init__(self, files: list[dict], file_info: dict, download_url: str):
        self._files = files
        self._file_info = file_info
        self._download_url = download_url

    def list_files(self, folder_id: str = "0", **kwargs):
        return {"data": {"list": self._files}}

    def get_file_info(self, file_id: str):
        return self._file_info

    def get_download_url(self, file_id: str):
        return self._download_url


@pytest.mark.asyncio
async def test_resolve_saved_file_ids_converts_dict(quark, cipher, monkeypatch):
    """list_files 返回原始 dict，resolve 必须转换为 ShareFile 后匹配。"""
    provider = QuarkProvider(cipher=cipher)
    fake = _FakeQuarkClient(
        files=[
            {"fid": "new1", "file_name": "a.txt", "dir": False},
            {"fid": "new2", "file_name": "b.txt", "dir": False},
        ],
        file_info={},
        download_url="",
    )
    acct = Account(
        id="a",
        provider="quark",
        label="x",
        encrypted_credential=cipher.encrypt("cookie"),
    )
    monkeypatch.setattr(provider, "_client_for", lambda a: fake)
    resolved = await provider.resolve_saved_file_ids(acct, None, ["a.txt"])
    assert resolved == ["new1"]


@pytest.mark.asyncio
async def test_get_download_info_reads_file_info_name(quark, cipher, monkeypatch):
    """get_file_info 返回单个 dict，直接取 file_name。"""
    provider = QuarkProvider(cipher=cipher)
    fake = _FakeQuarkClient(
        files=[],
        file_info={"fid": "new1", "file_name": "real-name.bin"},
        download_url="https://download.example.com/x",
    )
    acct = Account(
        id="a",
        provider="quark",
        label="x",
        encrypted_credential=cipher.encrypt("cookie"),
    )
    monkeypatch.setattr(provider, "_client_for", lambda a: fake)
    info = await provider.get_download_info(acct, "new1")
    assert info["url"] == "https://download.example.com/x"
    assert info["name"] == "real-name.bin"
