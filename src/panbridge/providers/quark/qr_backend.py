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

import httpx

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
        cookie = await self._exchange_cookie(service_ticket)
        if not cookie:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "failed to exchange service ticket for quark cookie",
                retryable=False,
            )
        return QrCredential(credential=cookie, label="quark-qr")

    async def _exchange_cookie(self, service_ticket: str) -> str:
        """用 service_ticket 换登录 Cookie（不复用 QuarkPan 的文件写入逻辑）。

        等价于 QuarkPan `_get_user_info_and_cookies` 的网络行为：
        请求 `https://pan.quark.cn/account/info?st=<ticket>&lw=scan`，
        响应的 Set-Cookie 会被写入独立 httpx client 的 cookie jar。
        这里不写任何文件，只提取 quark.cn 域名的 Cookie 拼成字符串。
        """
        async with httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/139.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
            },
        ) as client:
            resp = await client.get(
                "https://pan.quark.cn/account/info",
                params={"st": service_ticket, "lw": "scan"},
            )
            if resp.status_code != 200:
                logger.warning(
                    "exchange cookie got status %s", resp.status_code
                )
                return ""

            cookies: list[str] = []
            for cookie in client.cookies.jar:
                if cookie.domain and "quark.cn" in cookie.domain:
                    cookies.append(f"{cookie.name}={cookie.value}")
            return "; ".join(cookies)
