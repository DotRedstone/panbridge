"""三个真实扫码后端（quark/aliyun/115）的协议模拟测试。

通过 mock SDK 的网络调用，验证每个后端的状态流转：
- start 返回二维码
- check 的 waiting/scanned/success/expired/cancelled 映射
- finish 换取凭据（refresh_token / cookie）

不访问真实网络。
"""

from __future__ import annotations

import base64
import json

import pytest

# ---- 夸克后端 ----

@pytest.mark.asyncio
async def test_quark_backend_flow(monkeypatch):
    from panbridge.providers.quark.qr_backend import QuarkQrBackend

    class _FakeAPILogin:
        def __init__(self, timeout=300):
            self.timeout = timeout
            self._ticket = None

        def get_qr_code(self):
            return "qr-token", "https://su.quark.cn/fake?token=qr-token"

        def check_login_status(self, qr_token):
            if self._ticket:
                return {
                    "status": 2000000,
                    "message": "ok",
                    "data": {
                        "members": {"service_ticket": self._ticket}
                    },
                }
            return None

    fake = _FakeAPILogin()
    monkeypatch.setattr(
        "panbridge.providers.quark.qr_backend.APILogin",
        lambda **kw: fake,
    )

    backend = QuarkQrBackend()
    ticket = await backend.start()
    assert ticket.qr_url.startswith("https://su.quark.cn/")

    # 未扫码 → waiting
    assert (await backend.check(ticket)).status == "waiting"

    # 扫码成功 → success，且暂存 service_ticket
    fake._ticket = "fake-service-ticket"
    status = await backend.check(ticket)
    assert status.status == "success"
    assert ticket.state["service_ticket"] == "fake-service-ticket"

    # finish：拦截换 Cookie 网络请求
    async def _fake_exchange(ticket, service_ticket):
        return "__kps=abc; __uid=def"

    monkeypatch.setattr(backend, "_exchange_cookie", _fake_exchange)
    cred = await backend.finish(ticket)
    assert cred.credential == "__kps=abc; __uid=def"
    assert cred.label == "quark-qr"


# ---- 阿里云盘后端 ----

class _FakeAliyunResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _aliyun_confirm_resp(refresh_token: str):
    biz = json.dumps(
        {"pds_login_result": {"refreshToken": refresh_token}}
    ).encode("gb18030")
    biz_ext = base64.b64encode(biz).decode()
    return _FakeAliyunResponse(
        {
            "content": {
                "data": {"qrCodeStatus": "CONFIRMED", "bizExt": biz_ext}
            }
        }
    )


class _FakeAliyunClient:
    """模拟 aliyun 登录的 httpx 客户端。"""

    def __init__(self, refresh_token="refresh-token-123"):
        self.refresh_token = refresh_token
        self.closed = False
        self._status = "NEW"

    async def get(self, url, params=None):
        if url.endswith("generate.do"):
            return _FakeAliyunResponse(
                {
                    "content": {
                        "data": {
                            "codeContent": "https://aliyundrive.com/qr/fake",
                            "t": "x",
                        }
                    }
                }
            )
        return _FakeAliyunResponse({"content": {"data": {}}})

    async def post(self, url, data=None, params=None):
        if url.endswith("query.do"):
            return _FakeAliyunResponse(
                {
                    "content": {
                        "data": {
                            "qrCodeStatus": self._status,
                            "bizExt": base64.b64encode(
                                json.dumps(
                                    {
                                        "pds_login_result": {
                                            "refreshToken": self.refresh_token
                                        }
                                    }
                                ).encode("gb18030")
                            ).decode(),
                        }
                    }
                }
            )
        return _FakeAliyunResponse({"content": {"data": {}}})

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_aliyun_backend_flow(monkeypatch):
    import httpx

    from panbridge.providers.aliyun.qr_backend import AliyunQrBackend

    fake = _FakeAliyunClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)

    backend = AliyunQrBackend()
    ticket = await backend.start()
    assert ticket.qr_url == "https://aliyundrive.com/qr/fake"

    # NEW → waiting
    assert (await backend.check(ticket)).status == "waiting"

    # SCANED → scanned
    fake._status = "SCANED"
    assert (await backend.check(ticket)).status == "scanned"

    # CONFIRMED → success，且暂存 refresh_token
    fake._status = "CONFIRMED"
    status = await backend.check(ticket)
    assert status.status == "success"
    assert ticket.state["refresh_token"] == "refresh-token-123"

    cred = await backend.finish(ticket)
    assert cred.credential == "refresh-token-123"
    assert cred.label == "aliyun-qr"
    # finish 关闭 client，避免连接泄漏
    assert fake.closed is True


@pytest.mark.asyncio
async def test_aliyun_backend_expired(monkeypatch):
    import httpx

    from panbridge.providers.aliyun.qr_backend import AliyunQrBackend

    fake = _FakeAliyunClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)

    backend = AliyunQrBackend()
    ticket = await backend.start()
    fake._status = "EXPIRED"
    assert (await backend.check(ticket)).status == "expired"


# ---- 115 后端 ----

class _Fake115Data:
    def __init__(self, status=0):
        self.status = status


@pytest.mark.asyncio
async def test_pan115_backend_flow(monkeypatch):
    from panbridge.providers.pan115.qr_backend import Pan115QrBackend

    state = {"status": 0}

    async def _token(app="web", async_=False):
        return {
            "state": True,
            "data": {
                "uid": "uid-115",
                "qrcode": "https://115.com/scan/dg-uid-115",
                "time": 1,
                "sign": "sign",
            },
        }

    async def _status(payload, async_=False):
        return {"state": True, "data": {"status": state["status"]}}

    async def _result(uid, app="alipaymini", async_=False):
        return {
            "state": True,
            "data": {"cookie": "UID=1; CID=2; SEID=3"},
        }

    monkeypatch.setattr(
        "panbridge.providers.pan115.qr_backend.P115Client.login_qrcode_token",
        staticmethod(_token),
    )
    monkeypatch.setattr(
        "panbridge.providers.pan115.qr_backend.P115Client.login_qrcode_scan_status",
        staticmethod(_status),
    )
    monkeypatch.setattr(
        "panbridge.providers.pan115.qr_backend.P115Client.login_qrcode_scan_result",
        staticmethod(_result),
    )

    backend = Pan115QrBackend()
    ticket = await backend.start()
    assert ticket.qr_url == "https://115.com/scan/dg-uid-115"

    # 0 → waiting
    assert (await backend.check(ticket)).status == "waiting"

    # 1 → scanned
    state["status"] = 1
    assert (await backend.check(ticket)).status == "scanned"

    # 2 → success
    state["status"] = 2
    assert (await backend.check(ticket)).status == "success"

    cred = await backend.finish(ticket)
    assert cred.credential == "UID=1; CID=2; SEID=3"
    assert cred.label == "115-qr"


@pytest.mark.asyncio
async def test_pan115_backend_status_codes(monkeypatch):
    from panbridge.providers.pan115.qr_backend import Pan115QrBackend

    state = {"status": -1}

    async def _token(app="web", async_=False):
        return {"state": True, "data": {"uid": "u", "qrcode": "https://115.com/scan/dg-u"}}

    async def _status(payload, async_=False):
        return {"state": True, "data": {"status": state["status"]}}

    monkeypatch.setattr(
        "panbridge.providers.pan115.qr_backend.P115Client.login_qrcode_token",
        staticmethod(_token),
    )
    monkeypatch.setattr(
        "panbridge.providers.pan115.qr_backend.P115Client.login_qrcode_scan_status",
        staticmethod(_status),
    )

    backend = Pan115QrBackend()
    ticket = await backend.start()

    # -1 → expired
    state["status"] = -1
    assert (await backend.check(ticket)).status == "expired"

    # -2 → cancelled
    state["status"] = -2
    assert (await backend.check(ticket)).status == "cancelled"


# ---- 123 云盘后端 ----


class _Fake123Response:
    def __init__(self, payload: dict, status: int = 200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _Fake123Client:
    """模拟 123 扫码登录的 httpx.AsyncClient。"""

    def __init__(self):
        self.uni_id = "uni-123"
        self.login_status = 0
        self.token = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def get(self, url, params=None, headers=None):
        if url.endswith("/qr-code/generate"):
            return _Fake123Response(
                {
                    "code": 0,
                    "data": {
                        "url": "https://yun.123pan.cn/wx-app-login.html",
                        "uniID": self.uni_id,
                    },
                }
            )
        if url.endswith("/qr-code/result"):
            if self.token:
                return _Fake123Response(
                    {
                        "code": 200,
                        "data": {"loginStatus": 3, "token": self.token},
                    }
                )
            return _Fake123Response(
                {"code": 0, "data": {"loginStatus": self.login_status}}
            )
        return _Fake123Response({"code": 500, "message": "unknown"}, 500)

    async def post(self, url, json=None, headers=None):
        return _Fake123Response({"code": 0, "data": {}})


@pytest.mark.asyncio
async def test_pan123_backend_flow(monkeypatch):
    import httpx

    from panbridge.providers.pan123.qr_backend import Pan123QrBackend

    fake = _Fake123Client()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)

    backend = Pan123QrBackend()
    ticket = await backend.start()
    assert ticket.state["uniID"] == "uni-123"
    assert "uniID=uni-123" in ticket.qr_url

    # 0 → waiting
    assert (await backend.check(ticket)).status == "waiting"

    # 1 → scanned
    fake.login_status = 1
    assert (await backend.check(ticket)).status == "scanned"

    # 2 → cancelled
    fake.login_status = 2
    assert (await backend.check(ticket)).status == "cancelled"

    # 4 → expired
    fake.login_status = 4
    assert (await backend.check(ticket)).status == "expired"

    # 3 + token → success，暂存 token
    fake.token = "test-access-token-123"
    status = await backend.check(ticket)
    assert status.status == "success"
    assert ticket.state["token"] == "test-access-token-123"

    cred = await backend.finish(ticket)
    assert cred.credential == "test-access-token-123"
    assert cred.label == "123-qr"


@pytest.mark.asyncio
async def test_pan123_backend_finish_before_confirm(monkeypatch):
    import httpx

    from panbridge.domain.errors import DomainError, ErrorCode
    from panbridge.providers.pan123.qr_backend import Pan123QrBackend

    fake = _Fake123Client()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)

    backend = Pan123QrBackend()
    ticket = await backend.start()
    with pytest.raises(DomainError) as exc:
        await backend.finish(ticket)
    assert exc.value.code == ErrorCode.ACTION_REQUIRED


# ---- UC 网盘后端 ----


class _FakeUcResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeUcCookie:
    def __init__(self, name: str, value: str, domain: str = "drive.uc.cn"):
        self.name = name
        self.value = value
        self.domain = domain


class _FakeUcClient:
    """模拟 UC 扫码登录的 httpx.AsyncClient（含 cookie jar）。"""

    def __init__(self):
        self.service_ticket = ""
        self.jar: list[_FakeUcCookie] = []

    @property
    def cookies(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def aclose(self):
        return None

    async def post(self, url, data=None, headers=None):
        if "getTokenForQrcodeLogin" in url:
            return _FakeUcResponse(
                {
                    "status": 2000000,
                    "message": "ok",
                    "data": {"members": {"token": "uc-token"}},
                }
            )
        if "getServiceTicketByQrcodeToken" in url:
            if self.service_ticket:
                return _FakeUcResponse(
                    {
                        "status": 2000000,
                        "message": "ok",
                        "data": {"members": {"service_ticket": self.service_ticket}},
                    }
                )
            return _FakeUcResponse({"status": 50004001, "message": "waiting"})
        return _FakeUcResponse({"status": 50004002, "message": "unknown"}, 400)

    async def get(self, url, params=None, headers=None):
        # 换 Cookie 阶段：模拟服务端下发 __puus 等鉴权 Cookie
        self.jar.append(_FakeUcCookie("__puus", "puus-value"))
        self.jar.append(_FakeUcCookie("__kps", "kps-value"))
        return _FakeUcResponse({"status": 2000000, "message": "ok"})


@pytest.mark.asyncio
async def test_uc_backend_flow(monkeypatch):
    import panbridge.providers.uc.qr_backend as uc_mod
    from panbridge.providers.uc.qr_backend import UcQrBackend

    fake = _FakeUcClient()
    monkeypatch.setattr(uc_mod.httpx, "AsyncClient", lambda *a, **kw: fake)

    backend = UcQrBackend()
    ticket = await backend.start()
    assert ticket.state["token"] == "uc-token"
    assert ticket.qr_url.startswith("https://su.uc.cn/")

    # 未扫码 → waiting
    assert (await backend.check(ticket)).status == "waiting"

    # 扫码成功 → success，暂存 service_ticket
    fake.service_ticket = "uc-service-ticket"
    status = await backend.check(ticket)
    assert status.status == "success"
    assert ticket.state["service_ticket"] == "uc-service-ticket"

    # finish：换 Cookie，返回含 __puus 的 cookie 字符串
    cred = await backend.finish(ticket)
    assert "__puus=puus-value" in cred.credential
    assert "__kps=kps-value" in cred.credential
    assert cred.label == "uc-qr"


@pytest.mark.asyncio
async def test_uc_backend_finish_before_confirm(monkeypatch):
    import panbridge.providers.uc.qr_backend as uc_mod
    from panbridge.domain.errors import DomainError, ErrorCode
    from panbridge.providers.uc.qr_backend import UcQrBackend

    fake = _FakeUcClient()
    monkeypatch.setattr(uc_mod.httpx, "AsyncClient", lambda *a, **kw: fake)

    backend = UcQrBackend()
    ticket = await backend.start()
    with pytest.raises(DomainError) as exc:
        await backend.finish(ticket)
    assert exc.value.code == ErrorCode.ACTION_REQUIRED


@pytest.mark.asyncio
async def test_uc_backend_expired_status(monkeypatch):
    """UC 扫码状态 50004002 映射为 expired。"""
    import panbridge.providers.uc.qr_backend as uc_mod
    from panbridge.providers.uc.qr_backend import UcQrBackend

    class _ExpiredClient(_FakeUcClient):
        async def post(self, url, data=None, headers=None):
            if "getServiceTicketByQrcodeToken" in url:
                return _FakeUcResponse({"status": 50004002, "message": "expired"})
            return await super().post(url, data=data, headers=headers)

    fake = _ExpiredClient()
    monkeypatch.setattr(uc_mod.httpx, "AsyncClient", lambda *a, **kw: fake)

    backend = UcQrBackend()
    ticket = await backend.start()
    assert (await backend.check(ticket)).status == "expired"
