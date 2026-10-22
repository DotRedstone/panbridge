"""123 云盘扫码登录后端。

实现 `QrAuthBackend` 协议，复用统一的扫码登录框架（CLI + REST API）。

协议（2026-10 实测确认，参考 p123client / AList 123 驱动）：

1. 生成二维码：`GET https://login.123pan.com/api/user/qr-code/generate`
   → `data.uniID` + `data.url`。二维码内容为：
   `{url}?env=production&uniID={uniID}&source=123pan&type=login`
2. 轮询结果：`GET https://login.123pan.com/api/user/qr-code/result?uniID=...`
   → `data.loginStatus`（0=等待扫码 / 1=已扫码 / 2=已取消 / 3=已登录 / 4=已失效），
   登录成功后返回体含 `data.token`（30 天有效期）。

安全约束（与夸克后端一致）：
- 全程无状态，仅把 `uniID` 放在 ticket.state 内存中，不落盘。
- 最终 token 通过 `QrCredential` 返回，由上层加密入库，不在本模块写文件。
"""

from __future__ import annotations

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

# 扫码登录接口域名（独立于分享/账号接口）。
_LOGIN_ROOT = "https://login.123pan.com"

_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "platform": "web",
}


class Pan123QrBackend(QrAuthBackend):
    """123 云盘扫码登录后端。"""

    name = "123"
    display_name = "123 云盘"

    async def start(self) -> QrTicket:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_LOGIN_ROOT}/api/user/qr-code/generate",
                headers=_HEADERS,
            )
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"123 qr-code generate returned {resp.status_code}",
                retryable=True,
            )
        data = resp.json()
        if data.get("code") not in (0, 200):
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "123 qr-code generate failed: "
                f"{data.get('message') or data.get('code')}",
                retryable=True,
            )
        inner = data.get("data") or {}
        uni_id = inner.get("uniID")
        if not uni_id:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "123 qr-code generate missing uniID",
                retryable=False,
            )
        qr_url = (
            f"{inner.get('url', '')}?env=production&uniID={uni_id}"
            f"&source=123pan&type=login"
        )
        return QrTicket(qr_url=qr_url, state={"uniID": uni_id})

    async def check(self, ticket: QrTicket) -> QrStatus:
        uni_id = ticket.state.get("uniID") if ticket.state else None
        if not uni_id:
            return QrStatus(status="failed")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_LOGIN_ROOT}/api/user/qr-code/result",
                params={"uniID": uni_id},
                headers=_HEADERS,
            )
        if resp.status_code != 200:
            # 网络/服务异常，保守返回 waiting，交由上层重试。
            return QrStatus(status="waiting")
        data = resp.json()
        inner = data.get("data") or {}
        login_status = inner.get("loginStatus")
        # loginStatus==3（已登录）且带 token 才算成功。
        # 注意：code==0 仅表示接口调用正常，不代表登录成功，不能据此
        # 提前返回 success（否则 loginStatus=0/1/2/4 时误判）。
        if login_status == 3:
            token = inner.get("token")
            if not token:
                return QrStatus(status="waiting")
            ticket.state["token"] = token
            return QrStatus(status="success", intermediate=token)
        # 其它状态映射到统一 QrStatus。
        status_map = {
            0: "waiting",
            1: "scanned",
            2: "cancelled",
            4: "expired",
        }
        return QrStatus(status=status_map.get(login_status, "waiting"))

    async def finish(self, ticket: QrTicket) -> QrCredential:
        token = (ticket.state or {}).get("token")
        if not token:
            raise DomainError(
                ErrorCode.ACTION_REQUIRED,
                "123 qr login not yet confirmed; scan the qr code first",
                retryable=False,
            )
        return QrCredential(credential=token, label="123-qr")
