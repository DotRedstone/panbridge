"""PikPak Provider 单元测试（mock httpx + 签名算法，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.providers.pikpak.provider import (
    PikPakProvider,
    captcha_sign,
)


def test_captcha_sign_format():
    sign = captcha_sign("cid", "1.0", "pkg", "dev", "1234567890")
    assert sign.startswith("1.")
    assert len(sign) == 34  # "1." + 32 hex md5


def test_captcha_sign_deterministic():
    a = captcha_sign("cid", "1.0", "pkg", "dev", "1234567890")
    b = captcha_sign("cid", "1.0", "pkg", "dev", "1234567890")
    assert a == b


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
        # 精确匹配路径（避免 /share 前缀误匹配 /share/detail）
        path = url.split("?")[0].split("mypikpak.com")[-1]
        for key, resp in self._routes.items():
            if path.endswith(key):
                return resp
        return _FakeResponse(404, {"error": {"code": 1}})

    async def post(self, url, json=None, headers=None):
        path = url.split("?")[0].split("mypikpak.com")[-1]
        for key, resp in self._routes.items():
            if path.endswith(key):
                return resp
        return _FakeResponse(404, {"error": {"code": 1}})


def _install_fake(monkeypatch, routes):
    import panbridge.providers.pikpak.provider as mod

    def fake_client(*a, **k):
        return _FakeAsyncClient(routes)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)


@pytest.mark.asyncio
async def test_inspect_share_maps_files(monkeypatch):
    routes = {
        "/v1/shield/captcha/init": _FakeResponse(
            200, {"captcha_token": "ct-123"}
        ),
        "/drive/v1/share": _FakeResponse(
            200,
            {"share_status": "OK", "pass_code_token": "pt-123"},
        ),
        "/drive/v1/share/detail": _FakeResponse(
            200,
            {
                "files": [
                    {"id": "f1", "name": "a.mkv", "kind": "drive#file", "size": 100},
                    {"id": "f2", "name": "dir", "kind": "drive#folder"},
                ]
            },
        ),
    }
    _install_fake(monkeypatch, routes)

    p = PikPakProvider()
    share = await p.inspect_share("share123", None)
    assert len(share.files) == 2
    assert share.files[0].name == "a.mkv"
    assert share.files[0].is_dir is False
    assert share.files[1].name == "dir"
    assert share.files[1].is_dir is True


@pytest.mark.asyncio
async def test_inspect_share_wrong_passcode(monkeypatch):
    routes = {
        "/v1/shield/captcha/init": _FakeResponse(200, {"captcha_token": "ct"}),
        "/drive/v1/share": _FakeResponse(
            200, {"share_status": "PASS_CODE_ERROR"}
        ),
    }
    _install_fake(monkeypatch, routes)

    p = PikPakProvider()
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("share123", "wrong")
    assert exc.value.code == ErrorCode.INVALID_PASSWORD


@pytest.mark.asyncio
async def test_inspect_share_prohibited(monkeypatch):
    routes = {
        "/v1/shield/captcha/init": _FakeResponse(200, {"captcha_token": "ct"}),
        "/drive/v1/share": _FakeResponse(
            200, {"share_status": "PROHIBITED"}
        ),
    }
    _install_fake(monkeypatch, routes)

    p = PikPakProvider()
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("share123", None)
    assert exc.value.code == ErrorCode.RATE_LIMITED


@pytest.mark.asyncio
async def test_resolve_file_download_url(monkeypatch):
    routes = {
        "/drive/v1/share/file_info": _FakeResponse(
            200,
            {
                "file_info": {
                    "web_content_link": "https://cdn.pikpak.com/x.mkv"
                }
            },
        )
    }
    _install_fake(monkeypatch, routes)

    p = PikPakProvider()
    url = await p.resolve_file_download_url("share123", "f1", "pt", "ct")
    assert url == "https://cdn.pikpak.com/x.mkv"


@pytest.mark.asyncio
async def test_save_shared_files_unverified():
    p = PikPakProvider()
    with pytest.raises(DomainError) as exc:
        await p.save_shared_files(None, "x", None, [], None, "skip")
    assert exc.value.code == ErrorCode.UNSUPPORTED_OPERATION
