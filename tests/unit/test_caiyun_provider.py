"""中国移动云盘 Provider 单元测试（mock httpx，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Account
from panbridge.providers.caiyun.protocol import (
    decode_share_response,
    encode_share_payload,
    mcloud_sign,
)
from panbridge.providers.caiyun.provider import CaiyunProvider


class _FakeResponse:
    def __init__(
        self, status: int = 200, data: dict | None = None, content: bytes = b""
    ):
        self.status_code = status
        self._data = data or {}
        self.content = content

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception("http error")


class _FakeAsyncClient:
    def __init__(self, routes: dict[str, _FakeResponse]):
        self._routes = routes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    def _match(self, url: str):
        for key, resp in sorted(
            self._routes.items(), key=lambda kv: len(kv[0]), reverse=True
        ):
            if key in url:
                return resp
        return _FakeResponse(404, {})

    async def get(self, url, params=None, headers=None):
        return self._match(url)

    async def post(self, url, json=None, params=None, headers=None, content=None):
        return self._match(url)


class _FakeCipher:
    def decrypt(self, token):
        # Basic base64("user:13800000000")
        import base64

        return base64.b64encode(b"user:13800000000").decode()


def _account() -> Account:
    return Account(
        id="a1", provider="caiyun", label="test", encrypted_credential="enc"
    )


def _install_fake(monkeypatch, routes):
    import panbridge.providers.caiyun.provider as mod

    def fake_client(*a, **k):
        return _FakeAsyncClient(routes)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)


# ---- 协议层测试 ----

def test_share_payload_roundtrip():
    payload = {"getOutLinkInfoReq": {"linkID": "abc", "passwd": "1234"}}
    encoded = encode_share_payload(payload)
    decoded = decode_share_response(encoded.encode("ascii"))
    assert decoded["getOutLinkInfoReq"]["linkID"] == "abc"


def test_decode_plaintext_json():
    raw = b'{"code":"0000"}'
    assert decode_share_response(raw)["code"] == "0000"


def test_mcloud_sign_format():
    sign = mcloud_sign({"a": 1})
    parts = sign.split(",")
    assert len(parts) == 3
    assert len(parts[1]) == 16  # nonce
    assert len(parts[2]) == 32  # md5 hex


# ---- Provider 测试 ----

@pytest.mark.asyncio
async def test_validate_account(monkeypatch):
    routes = {"/api/user/info": _FakeResponse(200, {})}
    _install_fake(monkeypatch, routes)
    p = CaiyunProvider(cipher=_FakeCipher())
    assert await p.validate_account(_account()) is True


@pytest.mark.asyncio
async def test_inspect_share_requires_login():
    p = CaiyunProvider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("abc", None)
    assert exc.value.code == ErrorCode.UNSUPPORTED_OPERATION


@pytest.mark.asyncio
async def test_list_share_files(monkeypatch):
    # 加密响应：data.caLst + coLst
    import json

    plain = {
        "code": "0000",
        "data": {
            "caLst": [{"caID": "d1", "caName": "dir"}],
            "coLst": [{"coID": "f1", "coName": "a.txt", "coSize": 10}],
        },
    }
    # 直接返回明文 JSON（协议允许错误/明文场景）
    routes = {
        "getOutLinkInfoV6": _FakeResponse(
            200, data=plain, content=json.dumps(plain).encode()
        )
    }
    _install_fake(monkeypatch, routes)
    p = CaiyunProvider(cipher=_FakeCipher())
    files = await p.list_share_files("abc", None, account=_account())
    assert len(files) == 2
    assert files[0].is_dir is True
    assert files[1].name == "a.txt"


@pytest.mark.asyncio
async def test_list_account_files(monkeypatch):
    routes = {
        "/file/list": _FakeResponse(
            200,
            {
                "code": "0000",
                "data": {
                    "items": [
                        {"fileId": "f1", "name": "a.txt", "type": "file", "size": 10},
                        {"fileId": "f2", "name": "dir", "type": "folder"},
                    ]
                },
            },
        )
    }
    _install_fake(monkeypatch, routes)
    p = CaiyunProvider(cipher=_FakeCipher())
    files = await p.list_account_files(_account())
    assert len(files) == 2
    assert files[0].name == "a.txt"
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


def test_parse_share_id():
    url = "https://yun.139.com/shareweb/#/w/i/abc123"
    assert CaiyunProvider._parse_share_id(url) == "abc123"
    assert CaiyunProvider._parse_share_id("abc123") == "abc123"
