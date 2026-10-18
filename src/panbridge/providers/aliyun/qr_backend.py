"""阿里云盘扫码登录后端。

复用 aligo（MIT）定义的登录端点常量与协议，但**用独立 httpx 客户端**
自行实现二维码登录的完整流程，隔离 aligo `Auth._login_by_qrcode` 的
副作用（阻塞轮询、终端打印、写 `~/.aligo/` 明文配置目录）。

协议流程（与 aligo `_login` 等价，端点常量来自 aligo.core.Config）：
1. GET `AUTH_HOST/v2/oauth/authorize`（建立 session，拿 SESSIONID）。
2. GET `PASSPORT_HOST/newlogin/qrcode/generate.do` → `codeContent`（二维码）
   与 `data`（轮询所需 payload）。
3. 轮询 POST `PASSPORT_HOST/newlogin/qrcode/query.do` → `qrCodeStatus`：
   NEW（未扫）/ SCANED（已扫待确认）/ CONFIRMED（已确认）/ 其他（过期）。
4. CONFIRMED 后，从响应 `content.data.bizExt`（base64 + gb18030）解析
   `pds_login_result.refreshToken`，作为最终凭据加密入库。

凭据为 refresh_token（长期有效），入库时加密，不写 aligo 默认目录。
"""

from __future__ import annotations

import base64
import json
import logging

import httpx

from panbridge.application.auth import (
    QrAuthBackend,
    QrCredential,
    QrStatus,
    QrTicket,
)
from panbridge.domain.errors import DomainError, ErrorCode

logger = logging.getLogger(__name__)

# aligo 登录端点常量（自 aligo.core.Config 抽取，避免导入 aligo 触发其副作用）
_AUTH_HOST = "https://auth.aliyundrive.com"
_PASSPORT_HOST = "https://passport.aliyundrive.com"
_V2_OAUTH_AUTHORIZE = "/v2/oauth/authorize"
_NEWLOGIN_QRCODE_GENERATE = "/newlogin/qrcode/generate.do"
_NEWLOGIN_QRCODE_QUERY = "/newlogin/qrcode/query.do"
_CLIENT_ID = "25dzX3vbYqktVxyX"
_UNI_PARAMS = {"appName": "aliyun_drive"}

_HEADERS = {
    "Referer": "https://aliyundrive.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}


class AliyunQrBackend(QrAuthBackend):
    """阿里云盘扫码登录后端（refresh_token 凭据）。"""

    name = "aliyun"
    display_name = "阿里云盘"

    async def start(self) -> QrTicket:
        client = httpx.AsyncClient(
            timeout=30.0, headers=_HEADERS, follow_redirects=True
        )
        # 建立 session，拿 SESSIONID cookie
        try:
            await client.get(
                _AUTH_HOST + _V2_OAUTH_AUTHORIZE,
                params={
                    "login_type": "custom",
                    "response_type": "code",
                    "redirect_uri": "https://www.aliyundrive.com/sign/callback",
                    "client_id": _CLIENT_ID,
                    "state": '{"origin":"file://"}',
                },
            )
            resp = await client.get(
                _PASSPORT_HOST + _NEWLOGIN_QRCODE_GENERATE,
                params=_UNI_PARAMS,
            )
            resp.raise_for_status()
            data = resp.json()["content"]["data"]
        except Exception as exc:  # noqa: BLE001
            await client.aclose()
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"aliyun qr generate failed: {exc.__class__.__name__}",
                retryable=True,
            ) from exc

        qr_url = data.get("codeContent")
        if not qr_url:
            await client.aclose()
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "aliyun qr generate returned empty codeContent",
                retryable=True,
            )

        # state 持有 httpx client 与轮询 payload；finish 用同一 client 解析。
        return QrTicket(
            qr_url=qr_url,
            state={"client": client, "data": data},
        )

    async def check(self, ticket: QrTicket) -> QrStatus:
        client: httpx.AsyncClient = ticket.state["client"]
        data = ticket.state["data"]
        try:
            resp = await client.post(
                _PASSPORT_HOST + _NEWLOGIN_QRCODE_QUERY,
                data=data,
                params=_UNI_PARAMS,
            )
            resp.raise_for_status()
            login_data = resp.json()["content"]["data"]
            status = login_data.get("qrCodeStatus")
        except Exception:  # noqa: BLE001
            # 单次轮询失败视为等待
            return QrStatus(status="waiting")

        if status == "NEW":
            return QrStatus(status="waiting")
        if status == "SCANED":
            return QrStatus(status="scanned")
        if status == "CONFIRMED":
            refresh_token = self._extract_refresh_token(resp)
            ticket.state["refresh_token"] = refresh_token
            return QrStatus(status="success", intermediate=refresh_token)
        # 其他（EXPIRED / 未知）
        return QrStatus(status="expired")

    async def finish(self, ticket: QrTicket) -> QrCredential:
        refresh_token = ticket.state.get("refresh_token")
        client: httpx.AsyncClient | None = ticket.state.get("client")
        try:
            if client is not None:
                await client.aclose()
        finally:
            ticket.state["client"] = None
        if not refresh_token:
            raise DomainError(
                ErrorCode.ACTION_REQUIRED,
                "aliyun qr login not yet confirmed; scan the qr code first",
                retryable=False,
            )
        return QrCredential(credential=refresh_token, label="aliyun-qr")

    async def cancel(self, ticket: QrTicket) -> None:
        client = ticket.state.get("client")
        if client is not None:
            await client.aclose()
            ticket.state["client"] = None

    @staticmethod
    def _extract_refresh_token(resp: httpx.Response) -> str:
        """从 CONFIRMED 响应解析 refreshToken。

        bizExt 为 base64(gb18030) 编码的 JSON，含
        `pds_login_result.refreshToken`。
        """
        try:
            biz_ext = resp.json()["content"]["data"]["bizExt"]
            decoded = base64.b64decode(biz_ext).decode("gb18030")
            payload = json.loads(decoded)
            return payload["pds_login_result"]["refreshToken"]
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "aliyun qr confirm response missing refreshToken",
                retryable=False,
            ) from exc
