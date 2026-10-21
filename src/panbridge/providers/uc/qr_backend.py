"""UC 网盘扫码登录后端。

UC 网盘与夸克网盘同为 UC 系产品，扫码登录协议几乎同源（CAS 协议），
但域名、client_id、UA 不同：

1. 生成 token：`POST api.open.uc.cn/cas/ajax/getTokenForQrcodeLogin`
   （body: client_id=381, v=1.2, request_id）→ `data.members.token`。
2. 二维码 URL：`https://su.uc.cn/1_n0ZCv?uc_param_str=...&token=<t>&
   client_id=381&uc_biz_str=...`。
3. 轮询：`POST api.open.uc.cn/cas/ajax/getServiceTicketByQrcodeToken`
   （body 含 token）→ status=2000000 时 `data.members.service_ticket`。
4. 换 Cookie：`GET drive.uc.cn/account/info?st=<service_ticket>` 拿基础
   Cookie，再访问 `pc-api.uc.cn/1/clouddrive/file/sort` 拿 `__puus`。

安全约束与夸克后端一致：全程复用同一个 httpx.Client 累积 Cookie，
不在本模块写任何明文文件，最终 Cookie 经 QrCredential 返回由上层加密入库。

参考：xiaoya-alist `glue_python/uc_cookie/uc_cookie.py`（MIT）。
"""

from __future__ import annotations

import logging
import time
import uuid

import httpx

from panbridge.application.auth import (
    QrAuthBackend,
    QrCredential,
    QrStatus,
    QrTicket,
)
from panbridge.domain.errors import DomainError, ErrorCode

logger = logging.getLogger(__name__)

# UC 网盘客户端 UA（与下载时一致，规避风控）。
_UC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) uc-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
    "Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"
)

_CAS_HOST = "https://api.open.uc.cn"
_QR_LOGIN_PATH = "/cas/ajax/getTokenForQrcodeLogin"
_QR_STATUS_PATH = "/cas/ajax/getServiceTicketByQrcodeToken"
_ACCOUNT_INFO_URL = "https://drive.uc.cn/account/info"
_PUUS_URL = (
    "https://pc-api.uc.cn/1/clouddrive/file/sort"
    "?pr=UCBrowser&fr=pc&pdir_fid=0&_page=1&_size=50&_fetch_total=1"
    "&_fetch_sub_dirs=0&_sort=file_type:asc,updated_at:desc"
)
_CLIENT_ID = "381"
_QR_BASE = "https://su.uc.cn/1_n0ZCv"


class UcQrBackend(QrAuthBackend):
    """UC 网盘扫码登录后端（Cookie 凭据）。"""

    name = "uc"
    display_name = "UC 网盘"

    async def start(self) -> QrTicket:
        client = httpx.AsyncClient(
            timeout=30.0, headers={"user-agent": _UC_UA}, follow_redirects=True
        )
        try:
            token = await self._get_qr_token(client)
            qr_url = self._build_qr_url(token)
        except DomainError:
            await client.aclose()
            raise
        except Exception as exc:  # noqa: BLE001
            await client.aclose()
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"uc qr generate failed: {exc.__class__.__name__}",
                retryable=True,
            ) from exc

        return QrTicket(
            qr_url=qr_url,
            state={"client": client, "token": token},
        )

    async def check(self, ticket: QrTicket) -> QrStatus:
        client: httpx.AsyncClient = ticket.state["client"]
        token = ticket.state["token"]
        try:
            resp = await client.post(
                f"{_CAS_HOST}{_QR_STATUS_PATH}",
                data={
                    "client_id": _CLIENT_ID,
                    "v": "1.2",
                    "request_id": str(uuid.uuid4()),
                    "token": token,
                },
            )
            data = resp.json()
        except Exception:  # noqa: BLE001
            return QrStatus(status="waiting")

        status = data.get("status")
        if status == 2000000:
            service_ticket = (
                data.get("data", {}).get("members", {}).get("service_ticket")
            )
            if not service_ticket:
                return QrStatus(status="waiting")
            ticket.state["service_ticket"] = service_ticket
            return QrStatus(status="success", intermediate=service_ticket)
        # 50004001 = 等待扫码；50004002 = 二维码失效/过期；其他视为等待。
        if status == 50004002:
            return QrStatus(status="expired")
        return QrStatus(status="waiting")

    async def finish(self, ticket: QrTicket) -> QrCredential:
        service_ticket = ticket.state.get("service_ticket")
        if not service_ticket:
            raise DomainError(
                ErrorCode.ACTION_REQUIRED,
                "uc qr login not yet confirmed; scan the qr code first",
                retryable=False,
            )
        cookie = await self._exchange_cookie(ticket, service_ticket)
        if not cookie:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "failed to exchange service ticket for uc cookie",
                retryable=False,
            )
        return QrCredential(credential=cookie, label="uc-qr")

    async def cancel(self, ticket: QrTicket) -> None:
        client = ticket.state.get("client")
        if client is not None:
            await client.aclose()
            ticket.state["client"] = None

    # ---- 内部 ----

    async def _get_qr_token(self, client: httpx.AsyncClient) -> str:
        resp = await client.post(
            f"{_CAS_HOST}{_QR_LOGIN_PATH}",
            data={
                "client_id": _CLIENT_ID,
                "v": "1.2",
                "request_id": str(int(time.time() * 1000)),
            },
        )
        data = resp.json()
        if data.get("status") != 2000000:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"uc getTokenForQrcodeLogin failed: {data.get('message') or data.get('status')}",
                retryable=True,
            )
        token = data.get("data", {}).get("members", {}).get("token")
        if not token:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "uc qr token missing in response",
                retryable=False,
            )
        return token

    @staticmethod
    def _build_qr_url(token: str) -> str:
        return (
            f"{_QR_BASE}?uc_param_str=dsdnfrpfbivesscpgimibtbmnijblauputogpintnwktprchmt"
            f"&token={token}&client_id={_CLIENT_ID}"
            "&uc_biz_str=S%3Acustom%7CC%3Atitlebar_fix"
        )

    async def _exchange_cookie(
        self, ticket: QrTicket, service_ticket: str
    ) -> str:
        """用 service_ticket 换完整登录 Cookie（含 __puus）。

        必须复用 start/check 阶段的同一个 client，因为 CAS 的鉴权 Cookie
        分多步、跨域名累积下发（api.open.uc.cn → drive.uc.cn → pc-api.uc.cn）。
        """
        client: httpx.AsyncClient = ticket.state["client"]

        # 第一步：drive.uc.cn/account/info 换基础鉴权 Cookie。
        try:
            await client.get(
                _ACCOUNT_INFO_URL,
                params={"st": service_ticket},
            )
            # 第二步：访问 pc-api.uc.cn 触发 __puus 下发。
            await client.get(
                _PUUS_URL,
                headers={"referer": "https://drive.uc.cn/"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("uc cookie exchange http error: %s", exc)

        cookies: list[str] = []
        for ck in client.cookies.jar:
            if ck.domain and "uc.cn" in ck.domain:
                cookies.append(f"{ck.name}={ck.value}")
        return "; ".join(cookies)
