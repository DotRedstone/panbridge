"""conftest：测试夹具（临时数据库 + Mock Provider 环境）。"""

from __future__ import annotations

import pytest_asyncio

from panbridge.application.service import ApplicationService
from panbridge.downloads.http import HttpDownloader
from panbridge.persistence import (
    create_engine,
    create_sessionmaker,
    init_db,
)
from panbridge.providers.quark.mock import MockProvider
from panbridge.providers.registry import ProviderRegistry


@pytest_asyncio.fixture
async def service(tmp_path):
    engine = create_engine(
        f"sqlite+aiosqlite:///{tmp_path}/test.db"
    )
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)

    registry = ProviderRegistry()
    registry.register(MockProvider())

    downloader = HttpDownloader()
    svc = ApplicationService(registry, sessionmaker, downloader=downloader)
    yield svc
    await engine.dispose()


@pytest_asyncio.fixture
async def registry():
    reg = ProviderRegistry()
    reg.register(MockProvider())
    return reg
