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
    async def _fake_exchange(ticket_str):
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
