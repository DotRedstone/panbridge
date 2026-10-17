"""天翼云盘 Provider 单元测试（mock cloud189 + httpx，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Account
from panbridge.providers.cloud189.provider import Cloud189Provider


class _FakeCloud189:
    def __init__(self, config):
        self.config = config

    def get_disk_space_info(self):
        return {"res_code": 0}

    def get_all_files(self, folder_id):
        return [
            {"id": "1", "name": "a.txt", "size": 10, "is_folder": False},
            {"id": "2", "name": "dir", "is_folder": True},
        ]

    def download(self, file_id):
        return "https://download.189.cn/x"


class _FakeCipher:
    def __init__(self, plain: str = '{"username": "u", "password": "p"}'):
        self.plain = plain

    def decrypt(self, token):
        return self.plain


class _FakeResponse:
    def __init__(self, status: int = 200, data: dict | None = None):
        self.status_code = status
        self._data = data or {}

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
        for key in sorted(self._routes, key=len, reverse=True):
            if path.endswith(key):
                return self._routes[key]
        return _FakeResponse(404, {"res_code": "ShareInfoNotFound"})

    async def get(self, url, params=None, headers=None):
        path = url.split("?")[0]
        self.calls.append(("GET", path))
        return self._match(path)

    async def post(self, url, data=None, headers=None):
        path = url.split("?")[0]
        self.calls.append(("POST", path))
        return self._match(path)


def _install_httpx(monkeypatch, routes):
    import panbridge.providers.cloud189.provider as mod

    def fake_client(*a, **k):
        return _FakeAsyncClient(routes)

    monkeypatch.setattr(mod.httpx, "AsyncClient", fake_client)


def _account() -> Account:
    return Account(
        id="a1",
        provider="cloud189",
        label="test",
        encrypted_credential="enc",
    )


# ---- 账号操作（SDK） ----


@pytest.mark.asyncio
async def test_list_account_files(monkeypatch):
    import panbridge.providers.cloud189.provider as mod

    mod.Cloud189 = _FakeCloud189
    p = Cloud189Provider(cipher=_FakeCipher())
    files = await p.list_account_files(_account())
    assert len(files) == 2
    assert files[0].name == "a.txt"
    assert files[0].is_dir is False
    assert files[1].is_dir is True


@pytest.mark.asyncio
async def test_get_download_info(monkeypatch):
    import panbridge.providers.cloud189.provider as mod

    mod.Cloud189 = _FakeCloud189
    p = Cloud189Provider(cipher=_FakeCipher())
    info = await p.get_download_info(_account(), "1")
    assert info["url"] == "https://download.189.cn/x"


@pytest.mark.asyncio
async def test_validate_account_sdk(monkeypatch):
    import panbridge.providers.cloud189.provider as mod

    mod.Cloud189 = _FakeCloud189
    p = Cloud189Provider(cipher=_FakeCipher())
    assert await p.validate_account(_account()) is True


# ---- 分享解析（HTTP） ----


@pytest.mark.asyncio
async def test_inspect_share(monkeypatch):
    _install_httpx(
        monkeypatch,
        {
            "/api/open/share/getShareInfoByCodeV2.action": _FakeResponse(
                200,
                {
                    "res_code": 0,
                    "shareId": "S123",
                    "shareMode": 2,
                    "fileId": "0",
                    "shareName": "我的分享",
                    "accessCode": "",
                },
            ),
            "/api/open/share/listShareDir.action": _FakeResponse(
                200,
                {
                    "res_code": 0,
                    "fileListAO": {
                        "fileList": [
                            {"id": "f1", "name": "a.mkv", "size": 10, "isFolder": 0},
                            {"id": "d1", "name": "dir", "isFolder": 1},
                        ]
                    },
                },
            ),
        },
    )
    p = Cloud189Provider(cipher=_FakeCipher())
    share = await p.inspect_share("https://cloud.189.cn/t/abc123", None)
    assert share.provider == "cloud189"
    assert share.title == "我的分享"
    assert len(share.files) == 2
    assert share.files[0].name == "a.mkv"
    assert share.files[1].is_dir is True


@pytest.mark.asyncio
async def test_inspect_share_invalid(monkeypatch):
    _install_httpx(
        monkeypatch,
        {
            "/api/open/share/getShareInfoByCodeV2.action": _FakeResponse(
                200, {"res_code": "ShareInfoNotFound", "res_message": "不存在"}
            )
        },
    )
    p = Cloud189Provider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("bad", None)
    assert exc.value.code == ErrorCode.INVALID_SHARE


@pytest.mark.asyncio
async def test_inspect_share_wrong_password(monkeypatch):
    _install_httpx(
        monkeypatch,
        {
            "/api/open/share/getShareInfoByCodeV2.action": _FakeResponse(
                200, {"res_code": "NeedAccessCode", "res_message": "需要访问码"}
            )
        },
    )
    p = Cloud189Provider(cipher=_FakeCipher())
    with pytest.raises(DomainError) as exc:
        await p.inspect_share("abc", "wrong")
    assert exc.value.code == ErrorCode.INVALID_PASSWORD


# ---- 转存（HTTP + cookie） ----


@pytest.mark.asyncio
async def test_save_shared_files(monkeypatch):
    _install_httpx(
        monkeypatch,
        {
            "/api/open/share/getShareInfoByCodeV2.action": _FakeResponse(
                200, {"res_code": 0, "shareId": "S123", "shareMode": 2}
            ),
            "/api/open/batch/createBatchTask.action": _FakeResponse(
                200, {"res_code": 0, "taskId": "T1"}
            ),
        },
    )
    p = Cloud189Provider(
        cipher=_FakeCipher('{"cookie": "SSON=abc123"}')
    )
    result = await p.save_shared_files(
        _account(), "abc", None, ["f1"], "-11", "skip"
    )
    assert result.remote_task_id == "T1"
    # 转存后账号 id 与分享源 fid 不同命名空间，不伪造 saved_file_ids，
    # 交由 resolve_saved_file_ids 从目标目录匹配。
    assert result.saved_file_ids == []


@pytest.mark.asyncio
async def test_save_shared_files_no_cookie(monkeypatch):
    _install_httpx(monkeypatch, {})
    p = Cloud189Provider(cipher=_FakeCipher())  # 只有 username/password
    with pytest.raises(DomainError) as exc:
        await p.save_shared_files(_account(), "abc", None, ["f1"], None, "skip")
    assert exc.value.code == ErrorCode.ACCOUNT_EXPIRED


def test_extract_share_code():
    from panbridge.providers.cloud189.provider import _extract_share_code

    assert _extract_share_code("https://cloud.189.cn/t/abc123") == "abc123"
    assert _extract_share_code("abc123") == "abc123"


@pytest.mark.asyncio
async def test_resolve_saved_file_ids(monkeypatch):
    import panbridge.providers.cloud189.provider as mod

    mod.Cloud189 = _FakeCloud189
    p = Cloud189Provider(cipher=_FakeCipher())
    resolved = await p.resolve_saved_file_ids(
        _account(), "-11", ["a.txt", "missing.txt"]
    )
    assert resolved == ["1"]
