"""迅雷云盘 Provider 单元测试（mock httpx，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Account
from panbridge.providers.xunlei.provider import (
    XunleiProvider,
    captcha_sign,
    derive_device_id,
)


class _FakeCipher:
    def decrypt(self, token: str) -> str:
        return "fake-refresh-token"


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

    def _match(self, url: str):
        # 按 key 长度降序匹配，优先命中更精确的路由（如 /share/restore 先于 /share）
        for key, resp in sorted(
            self._routes.items(), key=lambda kv: len(kv[0]), reverse=True
        ):
            if key in url:
                return resp
        return _FakeResponse(404, {"error_code": 404})

    async def request(self, method, url, json=None, params=None, headers=None):
        return self._match(url)

    async def get(self, url, params=None, headers=None):
        return self._match(url)

    async def post(self, url, json=None, params=None, headers=None):
        return self._match(url)


def _install_fake(monkeypatch, routes):
    import panbridge.providers.xunlei.provider as mod

    def fake_client(*a, **k):
        return _FakeAsyncClient(routes)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)


def _account() -> Account:
    return Account(
        id="acc1",
        provider="xunlei",
        label="test",
        encrypted_credential="enc",
    )


def test_captcha_sign_format():
    sign = captcha_sign("cid", "1.0", "pkg", "dev", "1234567890")
    assert sign.startswith("1.")
    assert len(sign) == 34


def test_captcha_sign_deterministic():
    a = captcha_sign("cid", "1.0", "pkg", "dev", "1234567890")
    b = captcha_sign("cid", "1.0", "pkg", "dev", "1234567890")
    assert a == b


def test_derive_device_id():
    d = derive_device_id("rt")
    assert len(d) == 32


@pytest.mark.asyncio
async def test_validate_account(monkeypatch):
    routes = {
        "/v1/auth/token": _FakeResponse(
            200, {"data": {"access_token": "at", "expires_in": 7200}}
        )
    }
    _install_fake(monkeypatch, routes)
    p = XunleiProvider(cipher=_FakeCipher())
    assert await p.validate_account(_account()) is True


@pytest.mark.asyncio
async def test_validate_account_invalid(monkeypatch):
    routes = {
        "/v1/auth/token": _FakeResponse(
            400, {"error_description": "invalid refresh_token"}
        )
    }
    _install_fake(monkeypatch, routes)
    p = XunleiProvider(cipher=_FakeCipher())
    assert await p.validate_account(_account()) is False


@pytest.mark.asyncio
async def test_save_shared_files(monkeypatch):
    routes = {
        "/v1/auth/token": _FakeResponse(
            200, {"data": {"access_token": "at", "expires_in": 7200}}
        ),
        "/v1/shield/captcha/init": _FakeResponse(200, {"captcha_token": "ct"}),
        "/drive/v1/share": _FakeResponse(
            200,
            {
                "share_status": "OK",
                "pass_code_token": "pt",
                "files": [{"id": "f1", "name": "a.mkv", "kind": "drive#file"}],
            },
        ),
        "/drive/v1/share/restore": _FakeResponse(
            200, {"restore_task_id": "t1"}
        ),
        "/drive/v1/tasks": _FakeResponse(
            200,
            {
                "progress": 100,
                "params": {"trace_file_ids": '{"f1": "n1"}'},
            },
        ),
    }
    _install_fake(monkeypatch, routes)
    p = XunleiProvider(cipher=_FakeCipher())
    result = await p.save_shared_files(
        _account(), "share123", None, ["f1"], "", "skip"
    )
    assert result.saved_file_ids == ["n1"]


@pytest.mark.asyncio
async def test_inspect_share_requires_login():
    p = XunleiProvider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("share123", None)
    assert exc.value.code == ErrorCode.UNSUPPORTED_OPERATION


@pytest.mark.asyncio
async def test_save_wrong_passcode(monkeypatch):
    routes = {
        "/v1/auth/token": _FakeResponse(
            200, {"data": {"access_token": "at", "expires_in": 7200}}
        ),
        "/v1/shield/captcha/init": _FakeResponse(200, {"captcha_token": "ct"}),
        "/drive/v1/share": _FakeResponse(
            200, {"error_code": 1, "error_description": "pass_code invalid"}
        ),
    }
    _install_fake(monkeypatch, routes)
    p = XunleiProvider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.save_shared_files(_account(), "share123", "wrong", ["f1"], "", "skip")
    assert exc.value.code == ErrorCode.INVALID_PASSWORD


@pytest.mark.asyncio
async def test_list_account_files(monkeypatch):
    routes = {
        "/v1/auth/token": _FakeResponse(
            200, {"data": {"access_token": "at", "expires_in": 7200}}
        ),
        "/v1/shield/captcha/init": _FakeResponse(200, {"captcha_token": "ct"}),
        "/drive/v1/files": _FakeResponse(
            200,
            {
                "files": [
                    {"id": "a", "name": "x.mkv", "kind": "drive#file"},
                    {"id": "b", "name": "dir", "kind": "drive#folder"},
                ]
            },
        ),
    }
    _install_fake(monkeypatch, routes)
    p = XunleiProvider(cipher=_FakeCipher())
    files = await p.list_account_files(_account(), "")
    assert len(files) == 2
    assert files[1].is_dir is True


@pytest.mark.asyncio
async def test_get_download_info(monkeypatch):
    routes = {
        "/v1/auth/token": _FakeResponse(
            200, {"data": {"access_token": "at", "expires_in": 7200}}
        ),
        "/v1/shield/captcha/init": _FakeResponse(200, {"captcha_token": "ct"}),
        "/drive/v1/files/f1": _FakeResponse(
            200,
            {"web_content_link": "https://cdn.xunlei.com/x", "name": "a.mkv"},
        ),
    }
    _install_fake(monkeypatch, routes)
    p = XunleiProvider(cipher=_FakeCipher())
    info = await p.get_download_info(_account(), "f1")
    assert info["url"] == "https://cdn.xunlei.com/x"
    assert info["name"] == "a.mkv"


@pytest.mark.asyncio
async def test_get_download_info_via_medias(monkeypatch):
    routes = {
        "/v1/auth/token": _FakeResponse(
            200, {"data": {"access_token": "at", "expires_in": 7200}}
        ),
        "/v1/shield/captcha/init": _FakeResponse(200, {"captcha_token": "ct"}),
        "/drive/v1/files/f1": _FakeResponse(
            200,
            {
                "web_content_link": "",
                "medias": [{"link": {"url": "https://cdn.xunlei.com/v.m3u8"}}],
            },
        ),
    }
    _install_fake(monkeypatch, routes)
    p = XunleiProvider(cipher=_FakeCipher())
    info = await p.get_download_info(_account(), "f1")
    assert info["url"] == "https://cdn.xunlei.com/v.m3u8"
