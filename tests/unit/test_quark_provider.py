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


def test_map_quark_download_size_limit(quark):
    """夸克非会员单文件直链下载大小限制（23018）应映射为配额超限。"""
    from quark_client.exceptions import APIError

    exc = quark._map_quark_error(APIError("download file size limit[fid]"))
    assert exc.code == ErrorCode.QUOTA_EXCEEDED


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
    # 下载 headers 应包含 Referer，且不含 Cookie
    assert "headers" in info
    assert info["headers"]["Referer"] == "https://pan.quark.cn/"
    assert "Cookie" not in info["headers"]


class _CreateFolderClient:
    def create_folder(self, name, parent_id="0"):
        return {"status": 200, "data": {"fid": f"dir-{name}"}}


@pytest.mark.asyncio
async def test_create_folder_returns_fid(quark, cipher, monkeypatch):
    provider = QuarkProvider(cipher=cipher)
    acct = Account(
        id="a",
        provider="quark",
        label="x",
        encrypted_credential=cipher.encrypt("cookie"),
    )
    monkeypatch.setattr(provider, "_client_for", lambda a: _CreateFolderClient())
    fid = await provider.create_folder(acct, "test-dir", "0")
    assert fid == "dir-test-dir"


@pytest.mark.asyncio
async def test_create_folder_error_maps(quark, cipher, monkeypatch):
    from quark_client.exceptions import APIError

    provider = QuarkProvider(cipher=cipher)
    acct = Account(
        id="a",
        provider="quark",
        label="x",
        encrypted_credential=cipher.encrypt("cookie"),
    )

    class _Fail:
        def create_folder(self, name, parent_id="0"):
            raise APIError("HTTP错误: 404")

    monkeypatch.setattr(provider, "_client_for", lambda a: _Fail())
    with pytest.raises(DomainError) as exc:
        await provider.create_folder(acct, "x", "0")
    assert exc.value.code == ErrorCode.INVALID_SHARE


@pytest.mark.asyncio
async def test_resolve_saved_file_ids_ambiguous_raises(quark, cipher, monkeypatch):
    """同名文件匹配到多个时，必须报错而非静默选错。"""
    provider = QuarkProvider(cipher=cipher)
    fake = _FakeQuarkClient(
        files=[
            {"fid": "old1", "file_name": "a.txt", "dir": False},
            {"fid": "new1", "file_name": "a.txt", "dir": False},
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
    with pytest.raises(DomainError) as exc:
        await provider.resolve_saved_file_ids(acct, "0", ["a.txt"])
    assert exc.value.code == ErrorCode.INTERNAL_ERROR
    assert "ambiguous" in exc.value.message


class _ValidateClient:
    """mock QuarkClient：按预设返回值模拟 list_files 结果。"""

    def __init__(self, result):
        self._result = result

    def list_files(self, folder_id: str = "0", **kwargs):
        return self._result


@pytest.mark.asyncio
async def test_validate_account_list_files_ok(quark, cipher, monkeypatch):
    """list_files 返回 status=200 时，Cookie 判定有效。"""
    provider = QuarkProvider(cipher=cipher)
    acct = Account(
        id="a",
        provider="quark",
        label="x",
        encrypted_credential=cipher.encrypt("cookie"),
    )
    ok = {"status": 200, "code": 0, "data": {"list": []}}
    monkeypatch.setattr(provider, "_client_for", lambda a: _ValidateClient(ok))
    assert await provider.validate_account(acct) is True


@pytest.mark.asyncio
async def test_validate_account_error_result_is_invalid(quark, cipher, monkeypatch):
    """list_files 返回含 error 的 dict 时，判定失效（如 capacity 接口 404）。"""
    provider = QuarkProvider(cipher=cipher)
    acct = Account(
        id="a",
        provider="quark",
        label="x",
        encrypted_credential=cipher.encrypt("cookie"),
    )
    err = {"error": "HTTP错误: 404"}
    monkeypatch.setattr(provider, "_client_for", lambda a: _ValidateClient(err))
    assert await provider.validate_account(acct) is False


@pytest.mark.asyncio
async def test_validate_account_non_200_status_is_invalid(
    quark, cipher, monkeypatch
):
    """list_files 返回 status 非 200 时，判定失效。"""
    provider = QuarkProvider(cipher=cipher)
    acct = Account(
        id="a",
        provider="quark",
        label="x",
        encrypted_credential=cipher.encrypt("cookie"),
    )
    not_ok = {"status": 401, "message": "unauthorized"}
    monkeypatch.setattr(
        provider, "_client_for", lambda a: _ValidateClient(not_ok)
    )
    assert await provider.validate_account(acct) is False


@pytest.mark.asyncio
async def test_validate_account_exception_is_invalid(quark, cipher, monkeypatch):
    """list_files 抛异常时，判定失效而非抛出。"""
    provider = QuarkProvider(cipher=cipher)
    acct = Account(
        id="a",
        provider="quark",
        label="x",
        encrypted_credential=cipher.encrypt("cookie"),
    )

    class _Boom:
        def list_files(self, folder_id: str = "0", **kwargs):
            raise RuntimeError("network down")

    monkeypatch.setattr(provider, "_client_for", lambda a: _Boom())
    assert await provider.validate_account(acct) is False


class _FakeApiClient:
    """mock api_client：记录 get/post 调用，返回预设响应。"""

    def __init__(self, detail_list: list[dict], save_result: dict):
        self._detail_list = detail_list
        self._save_result = save_result
        self.get_calls: list[dict] = []
        self.post_calls: list[dict] = []

    def get(self, url, params=None, base_url=None, **kwargs):
        self.get_calls.append({"url": url, "params": params, "base_url": base_url})
        return {"status": 200, "data": {"list": self._detail_list}}

    def post(self, url, json_data=None, base_url=None, **kwargs):
        self.post_calls.append(
            {"url": url, "json_data": json_data, "base_url": base_url}
        )
        return self._save_result


class _FakeShareClient:
    def __init__(self, api_client):
        self.api_client = api_client

    def get_share_token(self, share_id, password=None):
        return "stoken-abc"


def test_save_shared_files_impl_builds_fid_token_list(quark):
    """转存必须携带与 fid_list 一一对应的 share_fid_token 列表。"""
    detail_list = [
        {"fid": "f1", "share_fid_token": "tok1"},
        {"fid": "f2", "share_fid_token": "tok2"},
    ]
    api = _FakeApiClient(detail_list, {"status": 200, "data": {"task_id": "t1"}})
    client = _FakeShareClient(api)

    result = quark._save_shared_files_impl(
        client, "share-1", "stoken-abc", ["f1", "f2"], "0"
    )

    # 验证 save 请求的 fid_token_list 与 fid_list 顺序一致
    save_call = api.post_calls[-1]
    assert save_call["url"] == "share/sharepage/save"
    assert save_call["json_data"]["fid_list"] == ["f1", "f2"]
    assert save_call["json_data"]["fid_token_list"] == ["tok1", "tok2"]
    assert result["data"]["task_id"] == "t1"


def test_save_shared_files_impl_missing_token_uses_placeholder(quark):
    """某文件缺失 share_fid_token 时以空串占位，保持列表等长。"""
    detail_list = [{"fid": "f1", "share_fid_token": "tok1"}]
    api = _FakeApiClient(detail_list, {"status": 200, "data": {"task_id": "t1"}})
    client = _FakeShareClient(api)

    quark._save_shared_files_impl(client, "share-1", "st", ["f1", "f2"], "0")

    save_call = api.post_calls[-1]
    assert save_call["json_data"]["fid_token_list"] == ["tok1", ""]
