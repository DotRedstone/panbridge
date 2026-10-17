"""115 网盘 Provider 单元测试（mock p115client，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import ErrorCode
from panbridge.domain.models import Account
from panbridge.providers.pan115.provider import Pan115Provider


class _FakeClient:
    def __init__(self, cookie: str):
        self.cookie = cookie

    def share_snap(self, payload):
        # 匿名解析分享，返回文件列表
        return {
            "state": True,
            "data": [
                {"fid": "f1", "n": "a.mkv", "s": 100, "pid": None},
                {"fid": "f2", "n": "dir", "pid": "0"},
            ],
        }

    def fs_files(self, payload):
        return {"state": True, "data": []}

    def fs_mkdir(self, payload):
        return {"state": True, "data": {"cid": "999"}}

    def share_receive(self, payload):
        return {"state": True, "data": {}}

    def download_url_app(self, payload):
        return {
            "state": True,
            "data": {"file.mkv": {"url": {"url": "https://dl.115.com/x"}}},
        }


def _make_provider(monkeypatch):
    import panbridge.providers.pan115.provider as mod

    mod.P115Client = _FakeClient
    return Pan115Provider(cipher=None)


def _account(credential: str = "UID=1;CID=2;SEID=3") -> Account:
    return Account(
        id="a1", provider="115", label="test", encrypted_credential=credential
    )


class _FakeCipher:
    def decrypt(self, token):
        return token


@pytest.mark.asyncio
async def test_inspect_share_anonymous(monkeypatch):
    p = _make_provider(monkeypatch)
    share = await p.inspect_share("sharecode123", None)
    assert len(share.files) == 2
    assert share.files[0].name == "a.mkv"
    assert share.files[0].is_dir is False
    assert share.files[1].name == "dir"
    assert share.files[1].is_dir is True


def test_map_file_fields():
    f = Pan115Provider._map_file({"fid": "123", "n": "a.txt", "s": 10})
    assert f.file_id == "123"
    assert f.name == "a.txt"
    assert f.size == 10


def test_map_error_password():
    err = Pan115Provider._map_error(Exception("receive_code 错误"))
    assert err.code == ErrorCode.INVALID_PASSWORD


def test_map_error_auth():
    err = Pan115Provider._map_error(Exception("401 未登录"))
    assert err.code == ErrorCode.ACCOUNT_EXPIRED


@pytest.mark.asyncio
async def test_resolve_saved_file_ids(monkeypatch):
    class _ResolveClient(_FakeClient):
        def fs_files(self, payload):
            return {
                "state": True,
                "data": [
                    {"fid": "new1", "n": "a.mkv", "s": 100},
                    {"fid": "new2", "n": "b.mkv", "s": 200},
                ],
            }

    import panbridge.providers.pan115.provider as mod

    mod.P115Client = _ResolveClient
    p = Pan115Provider(cipher=_FakeCipher())
    resolved = await p.resolve_saved_file_ids(
        _account(), "999", ["a.mkv", "missing.mkv"]
    )
    assert resolved == ["new1"]
