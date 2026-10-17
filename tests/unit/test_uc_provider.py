"""UC 网盘 Provider 单元测试（mock httpx，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Account
from panbridge.providers.uc.provider import UcProvider


class _FakeCipher:
    def decrypt(self, token: str) -> str:
        return "fake-cookie"


class _FakeResponse:
    def __init__(self, status: int = 200, data: dict | None = None):
        self.status_code = status
        self._data = data or {}

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
        for key, resp in self._routes.items():
            if path.endswith(key):
                return resp
        return _FakeResponse(404, {"status": 404})

    async def get(self, url, params=None, headers=None):
        path = url.split("?")[0]
        self.calls.append(("GET", path))
        return self._match(path)

    async def post(self, url, json=None, params=None, headers=None):
        path = url.split("?")[0]
        self.calls.append(("POST", path))
        return self._match(path)


def _install_fake(monkeypatch, routes):
    import panbridge.providers.uc.provider as mod

    def fake_client(*a, **k):
        return _FakeAsyncClient(routes)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)


def _account() -> Account:
    return Account(
        id="acc1",
        provider="uc",
        label="test",
        encrypted_credential="enc",
    )


@pytest.mark.asyncio
async def test_validate_account(monkeypatch):
    _install_fake(
        monkeypatch,
        {"/1/clouddrive/member": _FakeResponse(200, {"status": 200})},
    )
    p = UcProvider(cipher=_FakeCipher())
    assert await p.validate_account(_account()) is True


@pytest.mark.asyncio
async def test_validate_account_expired(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/1/clouddrive/member": _FakeResponse(
                200, {"status": 500, "message": "require login [guest]"}
            )
        },
    )
    p = UcProvider(cipher=_FakeCipher())
    assert await p.validate_account(_account()) is False


@pytest.mark.asyncio
async def test_inspect_share_requires_login():
    p = UcProvider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("abc123", None)
    assert exc.value.code == ErrorCode.UNSUPPORTED_OPERATION


@pytest.mark.asyncio
async def test_save_shared_files(monkeypatch):
    routes = {
        "/share/sharepage/v2/detail": _FakeResponse(
            200, {"status": 200, "data": {"token_info": {"stoken": "st ok"}}}
        ),
        "/share/sharepage/detail": _FakeResponse(
            200,
            {
                "status": 200,
                "data": {
                    "list": [
                        {
                            "fid": "f1",
                            "file_name": "a.txt",
                            "file_type": 1,
                            "share_fid_token": "tk1",
                        }
                    ]
                },
            },
        ),
        "/share/sharepage/save": _FakeResponse(
            200, {"status": 200, "data": {"task_id": "t1"}}
        ),
        "/clouddrive/task": _FakeResponse(
            200,
            {
                "status": 200,
                "data": {"status": 2, "save_as": {"save_as_top_fids": ["n1"]}},
            },
        ),
    }
    _install_fake(monkeypatch, routes)
    p = UcProvider(cipher=_FakeCipher())
    result = await p.save_shared_files(
        _account(), "abc123", None, ["f1"], "0", "skip"
    )
    assert result.saved_file_ids == ["n1"]
    assert result.remote_task_id == "t1"


@pytest.mark.asyncio
async def test_save_wrong_password(monkeypatch):
    routes = {
        "/share/sharepage/v2/detail": _FakeResponse(
            200, {"status": 500, "message": "密码错误"}
        )
    }
    _install_fake(monkeypatch, routes)
    p = UcProvider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.save_shared_files(_account(), "abc", "wrong", ["f1"], "0", "skip")
    assert exc.value.code == ErrorCode.INVALID_PASSWORD


@pytest.mark.asyncio
async def test_list_account_files(monkeypatch):
    routes = {
        "/clouddrive/file/sort": _FakeResponse(
            200,
            {
                "status": 200,
                "data": {
                    "list": [
                        {"fid": "a", "file_name": "x.mkv", "file_type": 1, "size": 10},
                        {"fid": "b", "file_name": "dir", "file_type": 0},
                    ]
                },
            },
        )
    }
    _install_fake(monkeypatch, routes)
    p = UcProvider(cipher=_FakeCipher())
    files = await p.list_account_files(_account(), "0")
    assert len(files) == 2
    assert files[0].name == "x.mkv"
    assert files[1].is_dir is True


def test_extract_pwd_id():
    assert UcProvider._extract_pwd_id("/s/abc123") == "abc123"
    assert UcProvider._extract_pwd_id("https://drive.uc.cn/s/xyz") == "xyz"


@pytest.mark.asyncio
async def test_get_download_info(monkeypatch):
    routes = {
        "/file/download": _FakeResponse(
            200,
            {
                "status": 200,
                "data": [{"download_url": "https://cdn.uc.cn/x", "file_name": "a.txt"}],
            },
        )
    }
    _install_fake(monkeypatch, routes)
    p = UcProvider(cipher=_FakeCipher())
    info = await p.get_download_info(_account(), "f1")
    assert info["url"] == "https://cdn.uc.cn/x"
    assert info["name"] == "a.txt"
