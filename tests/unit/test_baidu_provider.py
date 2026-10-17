"""百度网盘 Provider 单元测试（mock httpx，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.providers.baidu.provider import BaiduProvider, decode_seckey


def test_decode_seckey():
    assert decode_seckey("a-b~c_d") == "a+b=c/d"


def test_decode_seckey_noop():
    assert decode_seckey("plain") == "plain"


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

    async def post(self, url, content=None, headers=None):
        for path, resp in self._routes.items():
            if path in url:
                return resp
        return _FakeResponse(404, {"errno": -1})

    async def get(self, url, headers=None):
        for path, resp in self._routes.items():
            if path in url:
                return resp
        return _FakeResponse(404, {"errno": -1})


def _install_fake(monkeypatch, routes):
    import panbridge.providers.baidu.provider as mod

    def fake_client(*a, **k):
        return _FakeAsyncClient(routes)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)


@pytest.mark.asyncio
async def test_inspect_share_maps_files(monkeypatch):
    routes = {
        "/share/wxlist": _FakeResponse(
            200,
            {
                "errno": 0,
                "data": {
                    "title": "my-share",
                    "seckey": "a-b~c_d",
                    "shareid": "100",
                    "uk": "200",
                    "list": [
                        {
                            "fs_id": 1,
                            "server_filename": "a.txt",
                            "isdir": 0,
                            "size": 10,
                        },
                        {"fs_id": 2, "server_filename": "dir", "isdir": 1},
                    ],
                },
            },
        )
    }
    _install_fake(monkeypatch, routes)

    p = BaiduProvider(cipher=None)
    share = await p.inspect_share("abc123", "1234")
    assert share.title == "my-share"
    assert len(share.files) == 2
    assert share.files[0].name == "a.txt"
    assert share.files[0].is_dir is False
    assert share.files[1].name == "dir"
    assert share.files[1].is_dir is True


@pytest.mark.asyncio
async def test_inspect_share_wrong_password(monkeypatch):
    routes = {
        "/share/wxlist": _FakeResponse(
            200, {"errno": -12, "show_msg": "提取码错误"}
        )
    }
    _install_fake(monkeypatch, routes)

    p = BaiduProvider(cipher=None)
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("abc123", "wrong")
    assert exc.value.code == ErrorCode.INVALID_PASSWORD


@pytest.mark.asyncio
async def test_inspect_share_invalid(monkeypatch):
    routes = {
        "/share/wxlist": _FakeResponse(
            200, {"errno": 2, "show_msg": "分享不存在"}
        )
    }
    _install_fake(monkeypatch, routes)

    p = BaiduProvider(cipher=None)
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("abc123", None)
    assert exc.value.code == ErrorCode.INVALID_SHARE


@pytest.mark.asyncio
async def test_save_shared_files_unverified():
    p = BaiduProvider(cipher=None)
    with pytest.raises(DomainError) as exc:
        await p.save_shared_files(None, "x", None, [], None, "skip")
    assert exc.value.code == ErrorCode.UNSUPPORTED_OPERATION
