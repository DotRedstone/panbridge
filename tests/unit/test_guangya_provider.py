"""光鸭云盘 Provider 单元测试（mock httpx，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Account
from panbridge.providers.guangya.provider import GuangyaProvider


class _FakeCipher:
    def decrypt(self, token: str) -> str:
        # token 里带 "refresh" 则返回 JSON 含 refresh_token
        if token == "refresh-enc":
            return '{"refresh_token": "rt1"}'
        if token == "json-enc":
            return '{"access_token": "at1", "refresh_token": "rt1"}'
        return "plain-access-token"


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
        # 按 key 长度降序匹配，精确路由优先
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
    import panbridge.providers.guangya.provider as mod

    def fake_client(*a, **k):
        return _FakeAsyncClient(routes)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)


def _account(cred: str = "plain-access-token") -> Account:
    return Account(
        id="acc1",
        provider="guangya",
        label="test",
        encrypted_credential=cred,
    )


@pytest.mark.asyncio
async def test_inspect_share(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/drive/v1/share": _FakeResponse(
                200,
                {
                    "code": 0,
                    "files": [
                        {"id": "f1", "name": "a.mkv", "kind": "drive#file", "size": 10},
                        {"id": "d1", "name": "dir", "kind": "drive#folder"},
                    ],
                    "pass_code_token": "pct1",
                },
            )
        },
    )
    p = GuangyaProvider(cipher=_FakeCipher())
    share = await p.inspect_share("share1", None)
    assert share.provider == "guangya"
    assert len(share.files) == 2
    assert share.files[0].name == "a.mkv"
    assert share.files[1].is_dir is True


@pytest.mark.asyncio
async def test_inspect_share_invalid_password(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/drive/v1/share": _FakeResponse(
                200,
                {
                    "code": 1,
                    "error_code": "PASS_CODE_INVALID",
                    "message": "密码错误",
                },
            )
        },
    )
    p = GuangyaProvider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("share1", "wrong")
    assert exc.value.code == ErrorCode.INVALID_PASSWORD


@pytest.mark.asyncio
async def test_inspect_share_invalid(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/drive/v1/share": _FakeResponse(
                200, {"code": 404, "error_code": "NOT_FOUND", "message": "不存在"}
            )
        },
    )
    p = GuangyaProvider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("bad", None)
    assert exc.value.code == ErrorCode.INVALID_SHARE


@pytest.mark.asyncio
async def test_save_shared_files(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/drive/v1/share": _FakeResponse(
                200,
                {
                    "code": 0,
                    "files": [{"id": "f1", "name": "a.mkv", "kind": "drive#file"}],
                    "pass_code_token": "pct1",
                },
            ),
            "/drive/v1/share/restore": _FakeResponse(
                200, {"code": 0, "file_ids": ["new1"]}
            ),
        },
    )
    p = GuangyaProvider(cipher=_FakeCipher())
    result = await p.save_shared_files(
        _account(), "share1", None, ["f1"], None, "skip"
    )
    assert result.saved_file_ids == ["new1"]


@pytest.mark.asyncio
async def test_list_account_files(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/drive/v1/files": _FakeResponse(
                200,
                {
                    "code": 0,
                    "files": [
                        {"id": "f1", "name": "a.mkv", "kind": "drive#file", "size": 10},
                        {"id": "d1", "name": "dir", "kind": "drive#folder"},
                    ],
                },
            )
        },
    )
    p = GuangyaProvider(cipher=_FakeCipher())
    files = await p.list_account_files(_account(), "0")
    assert len(files) == 2
    assert files[0].name == "a.mkv"
    assert files[1].is_dir is True


@pytest.mark.asyncio
async def test_validate_account(monkeypatch):
    _install_fake(
        monkeypatch,
        {"/drive/v1/files": _FakeResponse(200, {"code": 0, "files": []})},
    )
    p = GuangyaProvider(cipher=_FakeCipher())
    assert await p.validate_account(_account()) is True


@pytest.mark.asyncio
async def test_create_folder(monkeypatch):
    _install_fake(
        monkeypatch,
        {"/drive/v1/files": _FakeResponse(200, {"code": 0, "id": "d2"})},
    )
    p = GuangyaProvider(cipher=_FakeCipher())
    fid = await p.create_folder(_account(), "myfolder", "0")
    assert fid == "d2"


@pytest.mark.asyncio
async def test_resolve_token_via_refresh(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/v1/auth/token": _FakeResponse(200, {"access_token": "new-at"}),
            "/drive/v1/files": _FakeResponse(200, {"code": 0, "files": []}),
        },
    )
    p = GuangyaProvider(cipher=_FakeCipher())
    assert await p.validate_account(_account("refresh-enc")) is True


@pytest.mark.asyncio
async def test_get_download_info(monkeypatch):
    _install_fake(
        monkeypatch,
        {
            "/drive/v1/files/f1/download": _FakeResponse(
                200, {"code": 0, "url": "https://cdn.guangyapan.com/x"}
            )
        },
    )
    p = GuangyaProvider(cipher=_FakeCipher())
    info = await p.get_download_info(_account(), "f1")
    assert info["url"] == "https://cdn.guangyapan.com/x"
