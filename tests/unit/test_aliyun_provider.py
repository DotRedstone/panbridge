"""阿里云盘 Provider 单元测试（mock httpx，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.providers.aliyun.provider import AliyunProvider


class _FakeResponse:
    def __init__(self, status: int, data: dict):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class _FakeAsyncClient:
    """模拟 httpx.AsyncClient，按 path 返回预设响应。"""

    def __init__(self, routes: dict[str, _FakeResponse]):
        self._routes = routes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, headers=None, json=None, timeout=None):
        for path, resp in self._routes.items():
            if path in url:
                return resp
        return _FakeResponse(404, {"code": "NotFound"})


@pytest.mark.asyncio
async def test_inspect_share_maps_files(monkeypatch):
    import panbridge.providers.aliyun.provider as mod

    token = "st-123"
    routes = {
        "/v2/share_link/get_share_token": _FakeResponse(
            200, {"share_token": token}
        ),
        "/adrive/v2/share_link/get_share_by_anonymous": _FakeResponse(
            200, {"share_name": "my-share"}
        ),
        "/adrive/v2/file/list_by_share": _FakeResponse(
            200,
            {
                "items": [
                    {
                        "file_id": "f1",
                        "name": "a.txt",
                        "type": "file",
                        "size": 100,
                    },
                    {
                        "file_id": "f2",
                        "name": "dir",
                        "type": "folder",
                        "size": None,
                    },
                ]
            },
        ),
    }

    def fake_client(*a, **k):
        return _FakeAsyncClient(routes)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)

    p = AliyunProvider(cipher=None)
    share = await p.inspect_share("abc", None)
    assert share.title == "my-share"
    assert len(share.files) == 2
    assert share.files[0].name == "a.txt"
    assert share.files[0].is_dir is False
    assert share.files[1].name == "dir"
    assert share.files[1].is_dir is True


@pytest.mark.asyncio
async def test_inspect_share_cancelled_maps_invalid(monkeypatch):
    import panbridge.providers.aliyun.provider as mod

    routes = {
        "/v2/share_link/get_share_token": _FakeResponse(
            200,
            {
                "code": "ShareLinkCancelled",
                "message": "share_link is cancelled by the creator",
            },
        ),
    }

    def fake_client(*a, **k):
        return _FakeAsyncClient(routes)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)

    p = AliyunProvider(cipher=None)
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("abc", None)
    assert exc.value.code == ErrorCode.INVALID_SHARE
