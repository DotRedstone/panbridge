"""夸克扫码登录后端。

复用 QuarkPan（MIT）的 `APILogin` 底层协议能力
（`get_qr_code` / `check_login_status`），但**隔离其副作用**：

- 不用 `APILogin.login()` / `wait_for_login()`：它们内部有 `print`、
  倒计时线程、`time.sleep` 阻塞轮询，且会把 `login_result.json`、
  `user_info.json` 写到 QuarkPan 自己的配置目录（明文凭据副作用）。
- 不用 `QuarkAuth`：它会把 Cookie 明文写到 `cookies.json`。

本后端只复用「获取二维码」与「单次查询扫码状态」两个无副作用的协议
方法，其余（service_ticket 换 Cookie）由本模块自行实现，做到：
1. 不阻塞 HTTP 事件循环。
2. 不重复保存明文凭据（Cookie 只在内存中短暂存在，最终加密进 DB）。
"""

from __future__ import annotations

import logging

from panbridge.application.auth import (
    QrAuthBackend,
    QrCredential,
    QrStatus,
    QrTicket,
)
from panbridge.domain.errors import DomainError, ErrorCode

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 依赖可选
    from quark_client.auth import APILogin
except ImportError:  # pragma: no cover
    APILogin = None  # type: ignore


class QuarkQrBackend(QrAuthBackend):
    """夸克扫码登录后端。"""

    name = "quark"
    display_name = "夸克网盘"

    def __init__(self, timeout: int = 300) -> None:
        if APILogin is None:  # pragma: no cover
            raise RuntimeError(
                "quarkpan 未安装，无法使用夸克扫码登录。pip install quarkpan"
            )
        self._timeout = timeout

    async def start(self) -> QrTicket:
        login_mgr = APILogin(timeout=self._timeout)
        import asyncio

        qr_token, qr_url = await asyncio.to_thread(login_mgr.get_qr_code)
        # state 持有 login_mgr 与 qr_token，供 check/finish 复用。
        return QrTicket(
            qr_url=qr_url,
            state={"login_mgr": login_mgr, "qr_token": qr_token},
        )

    async def check(self, ticket: QrTicket) -> QrStatus:
        login_mgr = ticket.state["login_mgr"]
        qr_token = ticket.state["qr_token"]

        import asyncio

        result = await asyncio.to_thread(
            login_mgr.check_login_status, qr_token
        )
        if result is None:
            # None 表示「仍在等待扫码」或「已扫码待确认」，无法区分，
            # 保守返回 waiting，由调用方继续轮询。
            return QrStatus(status="waiting")

        service_ticket = (
            result.get("data", {}).get("members", {}).get("service_ticket")
            if isinstance(result, dict)
            else None
        )
        if not service_ticket:
            return QrStatus(status="waiting")

        # 把 service_ticket 暂存到 state，供 finish 换 Cookie。
        ticket.state["service_ticket"] = service_ticket
        return QrStatus(status="success", intermediate=service_ticket)

    async def finish(self, ticket: QrTicket) -> QrCredential:
        service_ticket = ticket.state.get("service_ticket")
        if not service_ticket:
            raise DomainError(
                ErrorCode.ACTION_REQUIRED,
                "quark qr login not yet confirmed; scan the qr code first",
                retryable=False,
            )
        cookie = await self._exchange_cookie(ticket, service_ticket)
        if not cookie:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "failed to exchange service ticket for quark cookie",
                retryable=False,
            )
        return QrCredential(credential=cookie, label="quark-qr")

    async def _exchange_cookie(
        self, ticket: QrTicket, service_ticket: str
    ) -> str:
        """用 service_ticket 换登录 Cookie。

        关键：必须复用 `start()`/`check()` 阶段持有的同一个
        `login_mgr.client`（httpx.Client），因为夸克的登录 Cookie 是
        分多步、跨 `uop.quark.cn` 与 `pan.quark.cn` 两个域名累积下发的：

        - `get_qr_code`（uop.quark.cn/cas/ajax/getTokenForQrcodeLogin）
        - `check_login_status`（uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken）
        - `account/info?st=...&lw=scan`（pan.quark.cn，Set-Cookie 换鉴权 Cookie）

        若改用独立 client，会丢失前面阶段下发的 Cookie（如 `__puus` 等
        鉴权 Cookie），导致最终 Cookie 不完整、`validate_account` 判定失效。
        这里沿用 QuarkPan `login()` 的做法：全程复用 `login_mgr.client`，
        最后从该 client 的 cookie jar 提取 `quark.cn` 域 Cookie。
        """
        import asyncio

        login_mgr = ticket.state["login_mgr"]

        def _exchange() -> str:
            resp = login_mgr.client.get(
                "https://pan.quark.cn/account/info",
                params={"st": service_ticket, "lw": "scan"},
            )
            if resp.status_code != 200:
                logger.warning(
                    "exchange cookie got status %s", resp.status_code
                )
                return ""
            cookies: list[str] = []
            for cookie in login_mgr.client.cookies.jar:
                if cookie.domain and "quark.cn" in cookie.domain:
                    cookies.append(f"{cookie.name}={cookie.value}")
            return "; ".join(cookies)

        return await asyncio.to_thread(_exchange)
