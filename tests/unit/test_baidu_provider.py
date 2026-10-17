"""百度网盘 Provider 单元测试（mock httpx，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Account
from panbridge.providers.baidu.provider import BaiduProvider, decode_seckey


def test_decode_seckey():
    assert decode_seckey("a-b~c_d") == "a+b=c/d"


def test_decode_seckey_noop():
    assert decode_seckey("plain") == "plain"


class _FakeResponse:
    def __init__(self, status: int = 200, data: dict | None = None, text: str = ""):
        self.status_code = status
        self._data = data or {}
        self.text = text

    def json(self):
        return self._data


class _FakeCookieJar:
    def __iter__(self):
        return iter([])


class _FakeAsyncClient:
    def __init__(self, routes: dict[str, _FakeResponse]):
        self._routes = routes
        self.cookies = type("Cookies", (), {"jar": _FakeCookieJar()})()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, content=None, headers=None, params=None):
        for path, resp in self._routes.items():
            if path in url:
                return resp
        return _FakeResponse(404, {"errno": -1})

    async def get(self, url, headers=None, params=None):
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
async def test_save_shared_files(monkeypatch):
    routes = {
        "/api/gettemplatevariable": _FakeResponse(
            200, {"errno": 0, "data": {"bdstoken": "bt123"}}
        ),
        "/share/verify": _FakeResponse(200, {"errno": 0}),
        "/share/transfer": _FakeResponse(
            200, {"errno": 0, "data": {"task_id": "task-1"}}
        ),
        "/s/1abc123": _FakeResponse(
            200,
            {"errno": 0},
            text='<html>"shareid":"100","share_uk":"200"</html>',
        ),
    }
    _install_fake(monkeypatch, routes)

    p = BaiduProvider(cipher=_FakeCipher())
    result = await p.save_shared_files(
        _account(), "abc123", "1234", ["1", "2"], "/dest", "skip"
    )
    assert result.remote_task_id == "task-1"


@pytest.mark.asyncio
async def test_save_shared_files_requires_cookie():
    p = BaiduProvider(cipher=None)
    acc = Account(id="a1", provider="baidu", label="t", encrypted_credential=None)
    with pytest.raises(DomainError) as exc:
        await p.save_shared_files(acc, "x", None, ["1"], "/", "skip")
    assert exc.value.code == ErrorCode.ACCOUNT_EXPIRED


def _account():
    from panbridge.domain.models import Account

    return Account(
        id="a1", provider="baidu", label="test", encrypted_credential="enc"
    )


class _FakeCipher:
    def decrypt(self, token: str) -> str:
        return "BDUSS=xyz; STOKEN=abc"


@pytest.mark.asyncio
async def test_list_account_files(monkeypatch):
    routes = {
        "/api/gettemplatevariable": _FakeResponse(
            200, {"errno": 0, "data": {"bdstoken": "bt123"}}
        ),
        "/api/list": _FakeResponse(
            200,
            {
                "errno": 0,
                "list": [
                    {
                        "fs_id": "101",
                        "server_filename": "a.txt",
                        "isdir": 0,
                        "size": 10,
                    }
                ],
            },
        ),
    }
    _install_fake(monkeypatch, routes)
    p = BaiduProvider(cipher=_FakeCipher())
    files = await p.list_account_files(_account(), "/dest")
    assert len(files) == 1
    assert files[0].file_id == "101"
    assert files[0].name == "a.txt"


@pytest.mark.asyncio
async def test_get_download_info_account(monkeypatch):
    routes = {
        "/api/gettemplatevariable": _FakeResponse(
            200, {"errno": 0, "data": {"bdstoken": "bt123"}}
        ),
        "/api/download": _FakeResponse(
            200, {"errno": 0, "dlink": "https://d.pcs.baidu.com/x"}
        ),
    }
    _install_fake(monkeypatch, routes)
    p = BaiduProvider(cipher=_FakeCipher())
    info = await p.get_download_info(_account(), "101")
    assert info["url"] == "https://d.pcs.baidu.com/x"


@pytest.mark.asyncio
async def test_resolve_saved_file_ids(monkeypatch):
    routes = {
        "/api/gettemplatevariable": _FakeResponse(
            200, {"errno": 0, "data": {"bdstoken": "bt123"}}
        ),
        "/api/list": _FakeResponse(
            200,
            {
                "errno": 0,
                "list": [
                    {
                        "fs_id": "101",
                        "server_filename": "a.txt",
                        "isdir": 0,
                        "size": 10,
                    },
                    {
                        "fs_id": "102",
                        "server_filename": "b.txt",
                        "isdir": 0,
                        "size": 20,
                    },
                ],
            },
        ),
    }
    _install_fake(monkeypatch, routes)
    p = BaiduProvider(cipher=_FakeCipher())
    resolved = await p.resolve_saved_file_ids(
        _account(), "/dest", ["a.txt", "missing.txt"]
    )
    assert resolved == ["101"]
