"""123 云盘 Provider 单元测试（mock httpx + 签名算法，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Account
from panbridge.providers.pan123.provider import (
    Pan123Provider,
    _sign_path,
    _signed_url,
)


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
        return _FakeResponse(404, {"code": 404, "message": "not found"})

    async def post(self, url, json=None, headers=None):
        for path, resp in self._routes.items():
            if path in url:
                return resp
        return _FakeResponse(404, {"code": 404, "message": "not found"})


class _CapturingClient:
    """捕获最近一次 post 的 url/body/headers，供登录态下载测试断言。"""

    last_call: dict | None = None

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
        return _FakeResponse(404, {"code": 404, "message": "not found"})

    async def post(self, url, json=None, headers=None):
        _CapturingClient.last_call = {
            "url": url, "json": json, "headers": headers,
        }
        for path, resp in self._routes.items():
            if path in url:
                return resp
        return _FakeResponse(404, {"code": 404, "message": "not found"})


def _share_routes(files: list[dict]):
    return {
        "/gsb/s/share-key": _FakeResponse(
            200, {"info": {"code": 0, "data": {"UserID": 1625770}}}
        ),
        "/api/share/get": _FakeResponse(
            200, {"code": 0, "data": {"InfoList": files}}
        ),
    }


def _install(monkeypatch, routes):
    import panbridge.providers.pan123.provider as mod

    def fake_client(*a, **k):
        return _FakeAsyncClient(routes)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)


def test_sign_path_deterministic_shape():
    key, value = _sign_path("/b/api/share/get")
    assert key.isdigit()
    parts = value.split("-")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_signed_url_appends_query():
    url = _signed_url("https://yun.123pan.com/b/api/share/get")
    assert "?" in url
    assert url.startswith("https://yun.123pan.com/b/api/share/get?")


@pytest.mark.asyncio
async def test_inspect_share_maps_files(monkeypatch):
    _install(
        monkeypatch,
        _share_routes(
            [
                {
                    "FileId": 1001,
                    "FileName": "a.zip",
                    "Type": 0,
                    "Size": 2048,
                    "Etag": "e1",
                    "S3KeyFlag": "f1",
                },
                {
                    "FileId": 1002,
                    "FileName": "dir",
                    "Type": 1,
                    "Size": 0,
                },
            ]
        ),
    )
    p = Pan123Provider(cipher=None)
    share = await p.inspect_share("abc123", None)
    assert len(share.files) == 2
    assert share.files[0].name == "a.zip"
    assert share.files[0].is_dir is False
    assert share.files[1].name == "dir"
    assert share.files[1].is_dir is True


@pytest.mark.asyncio
async def test_inspect_share_invalid_password(monkeypatch):
    routes = {
        "/gsb/s/share-key": _FakeResponse(
            200, {"info": {"code": 0, "data": {"UserID": 1625770}}}
        ),
        "/api/share/get": _FakeResponse(
            200, {"code": 500, "message": "提取码错误"}
        ),
    }
    _install(monkeypatch, routes)
    p = Pan123Provider(cipher=None)
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("abc123", "wrong")
    assert exc.value.code == ErrorCode.INVALID_PASSWORD


@pytest.mark.asyncio
async def test_inspect_share_not_found(monkeypatch):
    routes = {
        "/gsb/s/share-key": _FakeResponse(
            200, {"info": {"code": 0, "data": {"UserID": 1625770}}}
        ),
        "/api/share/get": _FakeResponse(
            200, {"code": 404, "message": "分享不存在"}
        ),
    }
    _install(monkeypatch, routes)
    p = Pan123Provider(cipher=None)
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("abc123", None)
    assert exc.value.code == ErrorCode.INVALID_SHARE


@pytest.mark.asyncio
async def test_inspect_share_invalid_key(monkeypatch):
    routes = {
        "/gsb/s/share-key": _FakeResponse(
            200, {"info": {"code": 3, "message": "shareKey is invalid"}}
        )
    }
    _install(monkeypatch, routes)
    p = Pan123Provider(cipher=None)
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("badkey", None)
    assert exc.value.code == ErrorCode.INVALID_SHARE


@pytest.mark.asyncio
async def test_resolve_download_url_base64(monkeypatch):
    import base64

    real_url = "https://download.cdn.example.com/file.zip"
    encoded = base64.b64encode(real_url.encode()).decode()
    routes = {
        "/api/share/download/info": _FakeResponse(
            200,
            {
                "code": 0,
                "data": {
                    "DownloadUrl": f"https://yun.123pan.com/x?params={encoded}"
                },
            },
        )
    }
    _install(monkeypatch, routes)
    p = Pan123Provider(cipher=None)
    url = await p.resolve_share_download_url(
        "abc", None, {"fileId": 1001, "etag": "e", "s3keyFlag": "f", "size": 10}
    )
    assert url == real_url


@pytest.mark.asyncio
async def test_save_shared_files_unverified():
    p = Pan123Provider(cipher=None)
    with pytest.raises(DomainError) as exc:
        await p.save_shared_files(None, "x", None, [], None, "skip")
    assert exc.value.code == ErrorCode.UNSUPPORTED_OPERATION


@pytest.mark.asyncio
async def test_get_share_download_info_anonymous(monkeypatch):
    """匿名分享下载：解析列表 + 直链，返回 url 与文件名。"""
    import base64

    real_url = "https://download.cdn.example.com/a.zip"
    encoded = base64.b64encode(real_url.encode()).decode()
    routes = {
        "/gsb/s/share-key": _FakeResponse(
            200, {"info": {"code": 0, "data": {"UserID": 1625770}}}
        ),
        "/api/share/get": _FakeResponse(
            200,
            {
                "code": 0,
                "data": {
                    "InfoList": [
                        {
                            "FileId": 1001,
                            "FileName": "a.zip",
                            "Type": 0,
                            "Size": 2048,
                            "Etag": "e1",
                            "S3KeyFlag": "f1",
                        }
                    ]
                },
            },
        ),
        "/api/share/download/info": _FakeResponse(
            200,
            {
                "code": 0,
                "data": {
                    "DownloadUrl": f"https://yun.123pan.com/x?params={encoded}"
                },
            },
        ),
    }
    _install(monkeypatch, routes)
    p = Pan123Provider(cipher=None)
    info = await p.get_share_download_info("abc123", None, "1001")
    assert info["url"] == real_url
    assert info["name"] == "a.zip"
    assert info["size"] == 2048


@pytest.mark.asyncio
async def test_get_share_download_info_missing_file(monkeypatch):
    """目标文件不在分享列表时，如实报下载失败。"""
    routes = {
        "/gsb/s/share-key": _FakeResponse(
            200, {"info": {"code": 0, "data": {"UserID": 1625770}}}
        ),
        "/api/share/get": _FakeResponse(
            200, {"code": 0, "data": {"InfoList": []}}
        ),
    }
    _install(monkeypatch, routes)
    p = Pan123Provider(cipher=None)
    with pytest.raises(DomainError) as exc:
        await p.get_share_download_info("abc123", None, "999")
    assert exc.value.code == ErrorCode.DOWNLOAD_FAILED


def test_map_error_quota_exhausted():
    """5112 分享方流量包不足 → QUOTA_EXCEEDED（非风控/限速）。"""
    p = Pan123Provider(cipher=None)
    err = p._map_error({"code": 5112, "message": "分享方提取流量包不足"})
    assert err.code == ErrorCode.QUOTA_EXCEEDED
    assert err.retryable is False


def test_map_share_item_preserves_download_metadata():
    """_map_share_item 应保留 etag/s3keyFlag 到 raw，供匿名下载使用。"""
    item = {
        "FileId": 1001,
        "FileName": "a.zip",
        "Type": 0,
        "Size": 2048,
        "Etag": "e1",
        "S3KeyFlag": "f1",
    }
    f = Pan123Provider._map_share_item(item)
    assert f.raw["etag"] == "e1"
    assert f.raw["s3keyFlag"] == "f1"
    assert f.raw["fileId"] == "1001"


# ---- 登录态下载（platform=android 客户端标识）----


def _authed_account(cipher) -> Account:
    token = cipher.encrypt("test-access-token")
    return Account(
        id="a", provider="123", label="x", encrypted_credential=token
    )


def _capturing_install(monkeypatch, routes):
    import panbridge.providers.pan123.provider as mod

    def fake_client(*a, **k):
        return _CapturingClient(routes)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)


@pytest.mark.asyncio
async def test_get_share_download_info_authed(monkeypatch):
    """登录态下载：用 Etag+S3KeyFlag 调 /file/download_info，带 android 头。"""
    import base64

    real_url = "https://download.cdn.example.com/a.zip"
    encoded = base64.b64encode(real_url.encode()).decode()
    routes = {
        "/api/file/download_info": _FakeResponse(
            200,
            {
                "code": 0,
                "data": {
                    "DownloadUrl": f"https://yun.123pan.com/x?params={encoded}"
                },
            },
        ),
    }
    _capturing_install(monkeypatch, routes)
    from panbridge.infrastructure.crypto import CredentialCipher

    cipher = CredentialCipher("test-secret-key-for-panbridge")
    acct = _authed_account(cipher)
    p = Pan123Provider(cipher=cipher)

    from panbridge.domain.models import ShareFile

    target = ShareFile(
        file_id="1001",
        name="a.zip",
        size=2048,
        raw={"etag": "e1", "s3keyFlag": "f1", "size": 2048, "fileId": "1001"},
    )
    info = await p.get_share_download_info(
        "k73Mjv-3vrnA", "IlUH", "1001", file=target, account=acct
    )
    assert info["url"] == real_url
    assert info["name"] == "a.zip"
    assert info["checksum"] == "e1"
    # 断言请求走了 /file/download_info，且带 android 平台标识 + Bearer token。
    call = _CapturingClient.last_call
    assert call is not None
    assert "/api/file/download_info" in call["url"]
    assert call["headers"]["platform"] == "android"
    assert call["headers"]["authorization"] == "Bearer test-access-token"
    assert call["json"]["etag"] == "e1"
    assert call["json"]["s3keyFlag"] == "f1"


@pytest.mark.asyncio
async def test_get_share_download_info_authed_missing_metadata(monkeypatch):
    """登录态下载但缺 Etag/S3KeyFlag 时，如实报下载失败。"""
    from panbridge.infrastructure.crypto import CredentialCipher

    cipher = CredentialCipher("test-secret-key-for-panbridge")
    acct = _authed_account(cipher)
    p = Pan123Provider(cipher=cipher)

    from panbridge.domain.models import ShareFile

    target = ShareFile(file_id="1001", name="a.zip", size=2048, raw={})
    with pytest.raises(DomainError) as exc:
        await p.get_share_download_info(
            "k73Mjv-3vrnA", "IlUH", "1001", file=target, account=acct
        )
    assert exc.value.code == ErrorCode.DOWNLOAD_FAILED


@pytest.mark.asyncio
async def test_get_share_download_info_anonymous_still_works(monkeypatch):
    """无 account 时仍走匿名下载（向后兼容）。"""
    import base64

    real_url = "https://download.cdn.example.com/a.zip"
    encoded = base64.b64encode(real_url.encode()).decode()
    routes = {
        "/gsb/s/share-key": _FakeResponse(
            200, {"info": {"code": 0, "data": {"UserID": 1625770}}}
        ),
        "/api/share/get": _FakeResponse(
            200,
            {
                "code": 0,
                "data": {
                    "InfoList": [
                        {
                            "FileId": 1001,
                            "FileName": "a.zip",
                            "Type": 0,
                            "Size": 2048,
                            "Etag": "e1",
                            "S3KeyFlag": "f1",
                        }
                    ]
                },
            },
        ),
        "/api/share/download/info": _FakeResponse(
            200,
            {
                "code": 0,
                "data": {
                    "DownloadUrl": f"https://yun.123pan.com/x?params={encoded}"
                },
            },
        ),
    }
    _install(monkeypatch, routes)
    p = Pan123Provider(cipher=None)
    info = await p.get_share_download_info("abc123", None, "1001")
    assert info["url"] == real_url
