"""panbridge 应用入口。

只负责组装：创建配置、数据库、注册 Provider、下载器、应用服务，
并暴露 FastAPI app 与 Worker。业务逻辑不在本文件堆积。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from panbridge.api.router import build_router
from panbridge.application.auth import QuarkQrAuthService
from panbridge.application.auth_service import AuthService
from panbridge.application.service import ApplicationService
from panbridge.domain.errors import DomainError
from panbridge.downloads.http import HttpDownloader
from panbridge.infrastructure.config import Settings
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.persistence import (
    create_engine,
    create_sessionmaker,
    init_db,
)
from panbridge.providers.aliyun.provider import AliyunProvider
from panbridge.providers.baidu.provider import BaiduProvider
from panbridge.providers.caiyun.provider import CaiyunProvider
from panbridge.providers.cloud189.provider import Cloud189Provider
from panbridge.providers.lanzou.provider import LanzouProvider
from panbridge.providers.pan115.provider import Pan115Provider
from panbridge.providers.pan123.provider import Pan123Provider
from panbridge.providers.pikpak.provider import PikPakProvider
from panbridge.providers.quark.mock import MockProvider
from panbridge.providers.quark.provider import QuarkProvider
from panbridge.providers.registry import ProviderRegistry
from panbridge.workers.worker import Worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


def build_app(settings: Settings | None = None) -> tuple[FastAPI, Worker]:
    settings = settings or Settings.from_env()

    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)

    registry = ProviderRegistry()
    registry.register(MockProvider())
    # 真实夸克 Provider 需要解密器解密账号 Cookie
    # secret_key 已由 Settings.from_env 自动生成/加载，无需手工填写。
    cipher = CredentialCipher(settings.secret_key) if settings.secret_key else None
    registry.register(QuarkProvider(cipher=cipher))
    # 阿里云盘：分享解析/直链下载无需登录，仅转存需要 refresh_token
    registry.register(AliyunProvider(cipher=cipher))
    # 蓝奏云：匿名解析 + 直链下载，无需账号
    registry.register(LanzouProvider())
    # 123 云盘：匿名分享解析 + 直链下载，转存需账号
    registry.register(Pan123Provider(cipher=cipher))
    # 百度网盘：匿名解析分享列表，直链下载需 Cookie
    registry.register(BaiduProvider(cipher=cipher))
    # 115 网盘：匿名解析分享，转存/下载需 Cookie
    registry.register(Pan115Provider(cipher=cipher))
    # PikPak：匿名解析分享 + 直链下载，转存需账号
    registry.register(PikPakProvider())
    # 天翼云盘：账号文件列表 + 下载，分享解析需登录（未验证）
    registry.register(Cloud189Provider(cipher=cipher))
    # 中国移动云盘：账号文件列表 + 下载，分享解析未验证
    registry.register(CaiyunProvider(cipher=cipher))

    downloader = HttpDownloader(
        rate_limit=settings.download_rate_limit or None
    )

    service = ApplicationService(registry, sessionmaker, downloader=downloader)

    # 扫码登录认证（复用 QuarkPan 协议，隔离副作用）
    qr_auth = QuarkQrAuthService(sessionmaker, cipher) if cipher else None
    auth_service = AuthService(service, registry, qr_auth) if qr_auth else None

    worker = Worker(
        registry,
        sessionmaker,
        lease_seconds=settings.worker_lease_seconds,
        max_attempts=settings.max_retry_attempts,
        max_task_concurrency=settings.max_task_concurrency,
        downloader=downloader,
        download_dir=settings.download_dir,
    )

    app = FastAPI(title="PanBridge", version="0.1.0")
    app.state.settings = settings
    app.state.sessionmaker = sessionmaker
    app.state.worker = worker
    app.state.auth_service = auth_service
    app.state.cipher = cipher

    @app.on_event("startup")
    async def _startup() -> None:
        await init_db(engine)

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=_status_for(exc),
            content={
                "code": exc.code.value,
                "message": exc.message,
                "retryable": exc.retryable,
                "details": exc.details,
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    app.include_router(build_router(service, settings, auth_service))

    return app, worker


def _status_for(exc: DomainError) -> int:
    from panbridge.domain.errors import ErrorCode

    mapping = {
        ErrorCode.UNAUTHORIZED: 401,
        ErrorCode.TASK_NOT_FOUND: 404,
        ErrorCode.ACCOUNT_NOT_FOUND: 404,
        ErrorCode.INVALID_SHARE: 422,
        ErrorCode.INVALID_PASSWORD: 422,
        ErrorCode.INVALID_REQUEST: 400,
        ErrorCode.IDEMPOTENCY_CONFLICT: 409,
        ErrorCode.UNSUPPORTED_PROVIDER: 422,
        ErrorCode.UNSUPPORTED_OPERATION: 422,
        ErrorCode.TASK_NOT_CANCELLABLE: 409,
        ErrorCode.TASK_NOT_RETRYABLE: 409,
        ErrorCode.RATE_LIMITED: 429,
    }
    return mapping.get(exc.code, 500)


app, worker = build_app()
