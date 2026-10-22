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
async def test_inspect_share_anonymous(monkeypatch):
    """匿名解析：captcha 匿名签发 token + /drive/v1/share 免登录可读。"""
    routes = {
        "/v1/shield/captcha/init": _FakeResponse(200, {"captcha_token": "ct"}),
        "/drive/v1/share": _FakeResponse(
            200,
            {
                "share_status": "OK",
                "pass_code_token": "pt",
                "files": [
                    {"id": "d1", "name": "资料", "kind": "drive#folder", "size": "0"},
                    {"id": "f1", "name": "a.zip", "kind": "drive#file", "size": "123"},
                ],
            },
        ),
    }
    _install_fake(monkeypatch, routes)
    p = XunleiProvider(cipher=_FakeCipher())
    share = await p.inspect_share("share123", "abcd")
    assert share.provider == "xunlei"
    assert share.title == "资料"
    assert [f.name for f in share.files] == ["资料", "a.zip"]
    assert share.files[0].is_dir is True
    assert share.files[1].size == 123
    assert share.raw["pass_code_token"] == "pt"


@pytest.mark.asyncio
async def test_list_share_files_anonymous_subdir(monkeypatch):
    """匿名子目录遍历走 /drive/v1/share/detail，且带上 pass_code_token。"""
    routes = {
        "/v1/shield/captcha/init": _FakeResponse(200, {"captcha_token": "ct"}),
        "/drive/v1/share/detail": _FakeResponse(
            200,
            {
                "share_status": "OK",
                "files": [{"id": "f9", "name": "inner.zip", "kind": "drive#file"}],
            },
        ),
        "/drive/v1/share": _FakeResponse(
            200,
            {
                "share_status": "OK",
                "pass_code_token": "pt",
                "files": [{"id": "d1", "name": "dir", "kind": "drive#folder"}],
            },
        ),
    }
    _install_fake(monkeypatch, routes)
    p = XunleiProvider(cipher=_FakeCipher())
    root = await p.list_share_files("share123", "abcd")
    assert [f.name for f in root] == ["dir"]
    kids = await p.list_share_files("share123", "abcd", parent_id="d1")
    assert [f.name for f in kids] == ["inner.zip"]


@pytest.mark.asyncio
async def test_inspect_share_wrong_pass_code(monkeypatch):
    routes = {
        "/v1/shield/captcha/init": _FakeResponse(200, {"captcha_token": "ct"}),
        "/drive/v1/share": _FakeResponse(
            200, {"share_status": "PASS_CODE_ERROR", "files": []}
        ),
    }
    _install_fake(monkeypatch, routes)
    p = XunleiProvider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("share123", "wrong")
    assert exc.value.code == ErrorCode.INVALID_PASSWORD


@pytest.mark.asyncio
async def test_inspect_share_missing_pass_code(monkeypatch):
    routes = {
        "/v1/shield/captcha/init": _FakeResponse(200, {"captcha_token": "ct"}),
        "/drive/v1/share": _FakeResponse(
            200, {"share_status": "PASS_CODE_EMPTY", "files": []}
        ),
    }
    _install_fake(monkeypatch, routes)
    p = XunleiProvider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("share123", None)
    assert exc.value.code == ErrorCode.ACTION_REQUIRED


@pytest.mark.asyncio
async def test_anonymous_account_endpoint_requires_login(monkeypatch):
    """匿名访问账号端点（401）应映射 ACTION_REQUIRED，不伪造成功。"""
    routes = {
        "/v1/shield/captcha/init": _FakeResponse(200, {"captcha_token": "ct"}),
        "/drive/v1/share": _FakeResponse(
            401, {"error": "unauthenticated", "error_description": "帐号认证失败"}
        ),
    }
    _install_fake(monkeypatch, routes)
    p = XunleiProvider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("share123", None)
    assert exc.value.code == ErrorCode.ACTION_REQUIRED


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
