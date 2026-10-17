"""蓝奏云 Provider 单元测试（mock httpx，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.providers.lanzou.provider import LanzouProvider


class _FakeResponse:
    def __init__(self, status: int = 200, text: str = "", data: dict | None = None):
        self.status_code = status
        self.text = text
        self._data = data

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class _FakeAsyncClient:
    def __init__(self, get_response: _FakeResponse, post_response: _FakeResponse):
        self._get = get_response
        self._post = post_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def get(self, url, follow_redirects=False):
        return self._get

    async def post(self, url, data=None, headers=None):
        return self._post


_SHARE_PAGE = (
    "<html><head><title>testfile.zip - 蓝奏云</title></head>"
    "<body>"
    "<script>url : '/ajaxm.php?file=12345'</script>"
    "<div>action=downprocess&sign=SIGN123&p=</div>"
    "</body></html>"
)


def _install_fake(monkeypatch, get_resp, post_resp):
    import panbridge.providers.lanzou.provider as mod

    def fake_client(*a, **k):
        return _FakeAsyncClient(get_resp, post_resp)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)


@pytest.mark.asyncio
async def test_inspect_share_anonymous(monkeypatch):
    _install_fake(
        monkeypatch,
        _FakeResponse(200, text=_SHARE_PAGE),
        _FakeResponse(
            200,
            data={
                "zt": 1,
                "dom": "https://d0.lanzou.com",
                "url": "abc",
                "inf": "testfile.zip",
            },
        ),
    )

    p = LanzouProvider()
    share = await p.inspect_share("https://www.lanzou.com/abc123", None)
    assert share.title == "testfile.zip"
    assert len(share.files) == 1
    assert share.files[0].name == "testfile.zip"
    assert share.files[0].file_id == "https://d0.lanzou.com/file/abc"


@pytest.mark.asyncio
async def test_inspect_share_wrong_password(monkeypatch):
    _install_fake(
        monkeypatch,
        _FakeResponse(200, text=_SHARE_PAGE),
        _FakeResponse(200, data={"zt": 0, "info": "密码错误"}),
    )

    p = LanzouProvider()
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("https://www.lanzou.com/abc123", "wrong")
    assert exc.value.code == ErrorCode.INVALID_PASSWORD


@pytest.mark.asyncio
async def test_inspect_share_expired(monkeypatch):
    _install_fake(
        monkeypatch,
        _FakeResponse(200, text=_SHARE_PAGE),
        _FakeResponse(200, data={"zt": 0, "info": "文件已失效"}),
    )

    p = LanzouProvider()
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("https://www.lanzou.com/abc123", None)
    assert exc.value.code == ErrorCode.INVALID_SHARE


@pytest.mark.asyncio
async def test_get_download_info_with_direct_url():
    # file_id 已是直链时直接返回（蓝奏云 inspect_share 已把直链作为 file_id）
    p = LanzouProvider()
    info = await p.get_download_info(None, "https://d0.lanzou.com/file/abc")
    assert info["url"] == "https://d0.lanzou.com/file/abc"


@pytest.mark.asyncio
async def test_get_download_info_reparse_share_key(monkeypatch):
    # file_id 是 share key（非 URL）时重新解析
    _install_fake(
        monkeypatch,
        _FakeResponse(200, text=_SHARE_PAGE),
        _FakeResponse(
            200,
            data={
                "zt": 1,
                "dom": "https://d0.lanzou.com",
                "url": "abc",
                "inf": "testfile.zip",
            },
        ),
    )

    p = LanzouProvider()
    info = await p.get_download_info(None, "https://www.lanzou.com/abc123")
    assert info["url"] == "https://d0.lanzou.com/file/abc"
    assert info["name"] == "testfile.zip"


@pytest.mark.asyncio
async def test_save_shared_files_unsupported():
    p = LanzouProvider()
    with pytest.raises(DomainError) as exc:
        await p.save_shared_files(None, "x", None, [], None, "skip")
    assert exc.value.code == ErrorCode.UNSUPPORTED_OPERATION


@pytest.mark.asyncio
async def test_no_sign_raises_invalid_share(monkeypatch):
    _install_fake(
        monkeypatch,
        _FakeResponse(200, text="<html>no sign here</html>"),
        _FakeResponse(200, data={"zt": 1}),
    )

    p = LanzouProvider()
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("https://www.lanzou.com/abc123", None)
    assert exc.value.code == ErrorCode.INVALID_SHARE
