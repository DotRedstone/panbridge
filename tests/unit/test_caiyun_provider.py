"""中国移动云盘 Provider 单元测试（mock httpx，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Account
from panbridge.providers.caiyun.provider import CaiyunProvider


class _FakeResponse:
    def __init__(self, status: int = 200, data: dict | None = None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


class _FakeAsyncClient:
    def __init__(self, routes: dict[str, _FakeResponse]):
        self._routes = routes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def get(self, url, params=None, headers=None):
        for path, resp in self._routes.items():
            if path in url:
                return resp
        return _FakeResponse(404, {})


class _FakeCipher:
    def decrypt(self, token):
        return "the-token"


def _account() -> Account:
    return Account(
        id="a1", provider="caiyun", label="test", encrypted_credential="enc"
    )


def _install_fake(monkeypatch, routes):
    import panbridge.providers.caiyun.provider as mod

    def fake_client(*a, **k):
        return _FakeAsyncClient(routes)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)


@pytest.mark.asyncio
async def test_list_account_files(monkeypatch):
    routes = {
        "/api/file/list": _FakeResponse(
            200,
            {
                "code": "0000",
                "items": [
                    {"fileId": "f1", "name": "a.txt", "type": "file", "size": 10},
                    {"fileId": "f2", "name": "dir", "type": "folder"},
                ],
            },
        )
    }
    _install_fake(monkeypatch, routes)

    p = CaiyunProvider(cipher=_FakeCipher())
    files = await p.list_account_files(_account())
    assert len(files) == 2
    assert files[0].name == "a.txt"
    assert files[0].is_dir is False
    assert files[1].name == "dir"
    assert files[1].is_dir is True


@pytest.mark.asyncio
async def test_get_download_info(monkeypatch):
    routes = {
        "/api/file/download": _FakeResponse(
            200, {"url": "https://download.139.com/x", "name": "a.txt"}
        )
    }
    _install_fake(monkeypatch, routes)

    p = CaiyunProvider(cipher=_FakeCipher())
    info = await p.get_download_info(_account(), "f1")
    assert info["url"] == "https://download.139.com/x"
    assert info["name"] == "a.txt"


@pytest.mark.asyncio
async def test_validate_account(monkeypatch):
    routes = {"/api/user/info": _FakeResponse(200, {})}
    _install_fake(monkeypatch, routes)

    p = CaiyunProvider(cipher=_FakeCipher())
    assert await p.validate_account(_account()) is True


@pytest.mark.asyncio
async def test_inspect_share_unsupported():
    p = CaiyunProvider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("abc", None)
    assert exc.value.code == ErrorCode.UNSUPPORTED_OPERATION
