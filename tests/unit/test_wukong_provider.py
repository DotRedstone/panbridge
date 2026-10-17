"""悟空网盘 Provider 单元测试（mock httpx，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Account
from panbridge.providers.wukong.provider import WukongProvider


class _FakeCipher:
    def decrypt(self, token: str) -> str:
        if token == "json-enc":
            return '{"cookie": "c=1", "token": "tk1"}'
        return "fake-cookie"


class _FakeResponse:
    def __init__(self, status: int = 200, data: dict | None = None, headers=None):
        self.status_code = status
        self._data = data or {}
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._data


class _FakeAsyncClient:
    def __init__(self, routes: dict[str, _FakeResponse]):
        self._routes = routes
        self.calls: list[tuple[str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    def _match(self, path: str):
        for key in sorted(self._routes, key=len, reverse=True):
            if path.endswith(key):
                return self._routes[key]
        return _FakeResponse(404, {"code": 404, "message": "not found"})

    async def get(self, url, params=None, headers=None):
        path = url.split("?")[0]
        self.calls.append(("GET", path))
        return self._match(path)

    async def post(self, url, json=None, params=None, headers=None):
        path = url.split("?")[0]
        self.calls.append(("POST", path))
        return self._match(path)

    async def request(self, method, url, params=None, json=None, headers=None):
        path = url.split("?")[0]
        self.calls.append((method, path))
        return self._match(path)


def _install_fake(monkeypatch, routes):
    import panbridge.providers.wukong.provider as mod

    def fake_client(*a, **k):
        return _FakeAsyncClient(routes)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)


def _account(cred: str = "fake-cookie") -> Account:
    return Account(
        id="acc1",
        provider="wukong",
        label="test",
        encrypted_credential=cred,
    )


@pytest.mark.asyncio
async def test_inspect_share(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/api/v1/share/info": _FakeResponse(
                200,
                {
                    "code": 0,
                    "data": {
                        "title": "分享标题",
                        "files": [
                            {"file_id": "f1", "name": "a.mkv", "size": 10},
                            {"file_id": "d1", "name": "dir", "is_dir": True},
                        ],
                    },
                },
            )
        },
    )
    p = WukongProvider(cipher=_FakeCipher())
    share = await p.inspect_share("share1", None)
    assert share.provider == "wukong"
    assert share.title == "分享标题"
    assert len(share.files) == 2
    assert share.files[0].name == "a.mkv"
    assert share.files[1].is_dir is True


@pytest.mark.asyncio
async def test_inspect_share_invalid_password(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/api/v1/share/info": _FakeResponse(
                200, {"code": 1, "message": "提取码错误"}
            )
        },
    )
    p = WukongProvider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("share1", "wrong")
    assert exc.value.code == ErrorCode.INVALID_PASSWORD


@pytest.mark.asyncio
async def test_inspect_share_invalid(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/api/v1/share/info": _FakeResponse(
                200, {"code": 1, "message": "分享不存在"}
            )
        },
    )
    p = WukongProvider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("bad", None)
    assert exc.value.code == ErrorCode.INVALID_SHARE


@pytest.mark.asyncio
async def test_save_shared_files(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/api/v1/share/save": _FakeResponse(
                200, {"code": 0, "data": {"file_ids": ["new1"]}}
            )
        },
    )
    p = WukongProvider(cipher=_FakeCipher())
    result = await p.save_shared_files(
        _account(), "share1", None, ["f1"], None, "skip"
    )
    assert result.saved_file_ids == ["new1"]


@pytest.mark.asyncio
async def test_list_account_files(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/api/v1/file/list": _FakeResponse(
                200,
                {
                    "code": 0,
                    "data": {
                        "files": [
                            {"file_id": "f1", "name": "a.mkv", "size": 10},
                            {"file_id": "d1", "name": "dir", "is_dir": True},
                        ]
                    },
                },
            )
        },
    )
    p = WukongProvider(cipher=_FakeCipher())
    files = await p.list_account_files(_account(), "0")
    assert len(files) == 2
    assert files[0].name == "a.mkv"
    assert files[1].is_dir is True


@pytest.mark.asyncio
async def test_validate_account(monkeypatch):
    _install_fake(
        monkeypatch,
        {"/api/v1/file/list": _FakeResponse(200, {"code": 0, "files": []})},
    )
    p = WukongProvider(cipher=_FakeCipher())
    assert await p.validate_account(_account()) is True


@pytest.mark.asyncio
async def test_create_folder(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/api/v1/folder/create": _FakeResponse(
                200, {"code": 0, "data": {"file_id": "d2"}}
            )
        },
    )
    p = WukongProvider(cipher=_FakeCipher())
    fid = await p.create_folder(_account(), "myfolder", "0")
    assert fid == "d2"


@pytest.mark.asyncio
async def test_get_download_info(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/api/v1/file/download": _FakeResponse(
                200,
                {
                    "code": 0,
                    "data": {"url": "https://cdn.wkbrowser.com/x", "name": "a.mkv"},
                },
            )
        },
    )
    p = WukongProvider(cipher=_FakeCipher())
    info = await p.get_download_info(_account(), "f1")
    assert info["url"] == "https://cdn.wkbrowser.com/x"
    assert info["name"] == "a.mkv"
