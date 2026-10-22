"""蓝奏云 Provider 单元测试（mock httpx，不依赖网络）。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.providers.lanzou.provider import (
    LanzouProvider,
    _solve_acw_sc_v2,
)


class _FakeResponse:
    def __init__(
        self,
        status: int = 200,
        text: str = "",
        data: dict | None = None,
        headers: dict | None = None,
    ):
        self.status_code = status
        self.text = text
        self._data = data
        self.headers = headers or {}

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class _FakeAsyncClient:
    def __init__(self, get_response: _FakeResponse, post_response: _FakeResponse):
        self._get = get_response
        self._post = post_response
        self.cookies = {}  # 简化容器，支持 set(name, value, domain=...)

    def set(self, name: str, value: str, domain: str = "") -> None:
        self.cookies[name] = value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def get(self, url, follow_redirects=False, headers=None):
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

# 新版内嵌 sign 协议（2026）分享页：isngis 变量 + 绝对 ajaxfile 地址
_INLINE_SIGN_PAGE = (
    "<html><head><title>newfile.apk - 蓝奏云</title></head>"
    "<body>"
    "<script>var isngis = '';</script>"
    "<script>var isngis = 'SIGNABC_xyz';</script>"
    "<script>url : 'https://apifile.woozooo.com/ajaxfile.php?file=123'"
    "</script>"
    "</body></html>"
)

# 下载节点验证中转页：加密 file blob + sign，POST ajax.php 换真实直链
_INTERSTITIAL_PAGE = (
    "<html><body><script>"
    "data : { 'file':'ENCBLOB+xx/','el':el,'sign':'SGVsbG8=' },"
    "</script></body></html>"
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
async def test_inspect_share_inline_sign_protocol(monkeypatch):
    """新版内嵌 sign 协议（2026）：isngis + 绝对 ajaxfile 地址。"""
    _install_fake(
        monkeypatch,
        _FakeResponse(200, text=_INLINE_SIGN_PAGE),
        _FakeResponse(
            200,
            data={
                "zt": 1,
                "dom": "https://node.example.com",
                "url": "?ABCD/ef+g=",
                "inf": "newfile.apk",
            },
        ),
    )

    p = LanzouProvider()
    share = await p.inspect_share("https://wwawu.lanzouu.com/abc123", None)
    assert share.title == "newfile.apk"
    # 直链固定为 dom + /file/ + url（与页面 JS 拼接方式一致）
    assert (
        share.files[0].file_id
        == "https://node.example.com/file/?ABCD/ef+g="
    )


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
async def test_get_download_info_with_direct_url(monkeypatch):
    # file_id 已是直链且响应非 HTML（真实文件流）时原样返回
    _install_fake(
        monkeypatch,
        _FakeResponse(
            200,
            text=b"\x00\x01binary",
            headers={"content-type": "application/octet-stream"},
        ),
        _FakeResponse(200, data={}),
    )

    p = LanzouProvider()
    info = await p.get_download_info(None, "https://d0.lanzou.com/file/abc")
    assert info["url"] == "https://d0.lanzou.com/file/abc"


@pytest.mark.asyncio
async def test_get_download_info_resolves_interstitial(monkeypatch):
    """直链返回验证中转页时，二次解析出真实直链。"""
    _install_fake(
        monkeypatch,
        _FakeResponse(
            200,
            text=_INTERSTITIAL_PAGE,
            headers={"content-type": "text/html; charset=utf-8"},
        ),
        _FakeResponse(
            200, data={"zt": 1, "url": "https://cdn.example.com/real.bin"}
        ),
    )

    p = LanzouProvider()
    info = await p.get_download_info(
        None, "https://node.example.com/file/?ABC"
    )
    assert info["url"] == "https://cdn.example.com/real.bin"


@pytest.mark.asyncio
async def test_get_download_info_interstitial_sign_error(monkeypatch):
    """中转页验证失败（SignError 等）应映射为风控错误。"""
    _install_fake(
        monkeypatch,
        _FakeResponse(
            200,
            text=_INTERSTITIAL_PAGE,
            headers={"content-type": "text/html; charset=utf-8"},
        ),
        _FakeResponse(200, data={"zt": 1, "url": "?SignError"}),
    )

    p = LanzouProvider()
    with pytest.raises(DomainError):
        await p.get_download_info(None, "https://node.example.com/file/?ABC")


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


def test_solve_acw_sc_v2():
    """acw_sc__v2 反爬算法：arg1 重排 + XOR 密钥。"""
    html = (
        "<html><script>var arg1='F23C7AC7FDAD295172462574779DD3A425ADF0AE';"
        "(function(){...})()</script></html>"
    )
    result = _solve_acw_sc_v2(html)
    assert result is not None
    assert len(result) == 40
    assert all(c in "0123456789abcdef" for c in result)


def test_solve_acw_sc_v2_no_challenge():
    """无 arg1 的普通页面返回 None。"""
    assert _solve_acw_sc_v2("<html>normal page</html>") is None


def test_solve_acw_sc_v2_known_value():
    """用已知 arg1 验证算法确定性（与逆向参考一致）。"""
    # 构造一个 arg1，验证输出与手动计算一致。
    html = "var arg1='0000000000000000000000000000000000000000';"
    result = _solve_acw_sc_v2(html)
    # 全 0 的 arg1 重排后仍全 0，XOR 密钥即密钥本身。
    assert result == "3000176000856006061501533003690027800375"


def test_map_error_captcha():
    """下载节点验证码风控应返回 ACTION_REQUIRED。"""
    err = LanzouProvider._map_error(200, {"zt": 0, "url": "验证码错误"})
    assert err.code == ErrorCode.ACTION_REQUIRED


def test_map_error_network_abnormal():
    """网络异常需验证应返回 ACTION_REQUIRED。"""
    err = LanzouProvider._map_error(
        200, {"zt": 0, "info": "系统发现您的网络异常"}
    )
    assert err.code == ErrorCode.ACTION_REQUIRED
