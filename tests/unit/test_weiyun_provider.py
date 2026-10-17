"""腾讯微云 Provider 单元测试（mock httpx，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Account
from panbridge.providers.weiyun.provider import WeiyunProvider, build_body


class _FakeCipher:
    def decrypt(self, token: str) -> str:
        return "skey=abc123; wyctoken=999; uid=1"


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

    async def post(self, url, json=None, headers=None):
        for key, resp in self._routes.items():
            if key in url:
                return resp
        return _FakeResponse(404, {})


def _install_fake(monkeypatch, routes):
    import panbridge.providers.weiyun.provider as mod

    def fake_client(*a, **k):
        return _FakeAsyncClient(routes)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)


def _account() -> Account:
    return Account(
        id="a1", provider="weiyun", label="test", encrypted_credential="enc"
    )


def test_build_body_nested_json():
    body = build_body(12002, '".x.y_body":{"k":"v"}', skey="sk")
    # req_header/req_body 均为 JSON 字符串
    import json

    header = json.loads(body["req_header"])
    assert header["cmd"] == 12002
    assert header["appid"] == 30013
    req_body = json.loads(body["req_body"])
    assert (
        req_body["ReqMsg_body"]["ext_req_head"]["token_info"]["login_key_value"]
        == "sk"
    )
    assert req_body["ReqMsg_body"][".x.y_body"]["k"] == "v"


def test_parse_share_id():
    assert WeiyunProvider._parse_share_id("https://share.weiyun.com/abc123") == "abc123"
    assert WeiyunProvider._parse_share_id("abc123") == "abc123"


def test_skey_from_cookie():
    assert WeiyunProvider._skey_from_cookie("a=1; skey=xyz; b=2") == "xyz"
    assert WeiyunProvider._skey_from_cookie("a=1") is None


@pytest.mark.asyncio
async def test_inspect_share_anonymous(monkeypatch):
    routes = {
        "weiyunShareNoLogin/WeiyunShareView": _FakeResponse(
            200,
            {
                "data": {
                    "rsp_body": {
                        "RspMsg_body": {
                            "share_name": "test",
                            "file_list": [
                                {"file_id": "f1", "filename": "a.txt", "file_size": 10}
                            ],
                        }
                    }
                }
            },
        )
    }
    _install_fake(monkeypatch, routes)
    p = WeiyunProvider(cipher=_FakeCipher())
    share = await p.inspect_share("https://share.weiyun.com/abc123", None)
    assert share.title == "test"
    assert len(share.files) == 1
    assert share.files[0].name == "a.txt"


@pytest.mark.asyncio
async def test_inspect_share_wrong_pwd(monkeypatch):
    routes = {
        "weiyunShareNoLogin/WeiyunShareView": _FakeResponse(
            200, {"retcode": -30018, "msg": "密码错误"}
        )
    }
    _install_fake(monkeypatch, routes)
    p = WeiyunProvider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("abc123", "wrong")
    assert exc.value.code == ErrorCode.INVALID_PASSWORD


@pytest.mark.asyncio
async def test_save_shared_files(monkeypatch):
    routes = {
        "WeiyunShareView": _FakeResponse(
            200,
            {
                "data": {
                    "rsp_body": {
                        "RspMsg_body": {
                            "file_list": [
                                {"file_id": "f1", "filename": "a.txt", "file_size": 10}
                            ]
                        }
                    }
                }
            },
        ),
        "WeiyunSharePartSaveData": _FakeResponse(
            200,
            {
                "data": {
                    "rsp_body": {
                        "RspMsg_body": {"saved_file_list": [{"file_id": "n1"}]}
                    }
                }
            },
        ),
    }
    _install_fake(monkeypatch, routes)
    p = WeiyunProvider(cipher=_FakeCipher())
    result = await p.save_shared_files(
        _account(), "abc123", None, ["f1"], None, "skip"
    )
    assert result.saved_file_ids == ["n1"]


@pytest.mark.asyncio
async def test_save_requires_login():
    p = WeiyunProvider(cipher=None)
    acc = Account(id="a1", provider="weiyun", label="t", encrypted_credential=None)
    with pytest.raises(DomainError) as exc:
        await p.save_shared_files(acc, "abc", None, ["f1"], None, "skip")
    assert exc.value.code == ErrorCode.ACCOUNT_EXPIRED


@pytest.mark.asyncio
async def test_validate_account(monkeypatch):
    routes = {
        "DiskUserInfoGet": _FakeResponse(
            200, {"data": {"rsp_body": {"RspMsg_body": {"nickname": "u"}}}}
        )
    }
    _install_fake(monkeypatch, routes)
    p = WeiyunProvider(cipher=_FakeCipher())
    assert await p.validate_account(_account()) is True
