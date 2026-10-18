"""HTTP API → Worker 端到端集成测试。

验证「HTTP 创建任务 → Worker 领取执行 → 文件落盘 → 查询结果」的完整衔接。
这是核心目标「用户提供链接 → 统一任务 API → 自动转存/下载」的关键链路。
"""

from __future__ import annotations

import asyncio

import pytest_asyncio
from fastapi.testclient import TestClient

from panbridge.api.router import build_router
from panbridge.application.service import ApplicationService
from panbridge.infrastructure.config import Settings
from panbridge.persistence import (
    AccountRepository,
    create_engine,
    create_sessionmaker,
    init_db,
    session_scope,
)
from panbridge.providers.quark.mock import MockProvider
from panbridge.providers.registry import ProviderRegistry
from panbridge.workers.worker import Worker


class _FakeDownloader:
    """记录下载调用，不真正访问网络。"""

    def __init__(self):
        self.calls: list[str] = []

    async def download(self, url, destination, **kwargs):
        self.calls.append(destination)
        # 写一个非空文件，模拟真实落盘
        import os

        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as f:
            f.write(b"fake-content")
        yield {"downloaded": 12, "total": 12}


@pytest_asyncio.fixture
async def app_env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)

    registry = ProviderRegistry()
    registry.register(MockProvider())

    downloader = _FakeDownloader()
    service = ApplicationService(registry, sessionmaker, downloader=downloader)

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        api_key="",
        secret_key="test-secret",
        download_dir=str(tmp_path / "dl"),
    )

    app = build_router(service, settings)
    from fastapi import FastAPI

    fastapi_app = FastAPI()
    fastapi_app.include_router(app)
    fastapi_app.state.settings = settings

    worker = Worker(
        registry,
        sessionmaker,
        downloader=downloader,
        download_dir=str(tmp_path / "dl"),
        poll_interval=0.01,
    )

    yield fastapi_app, service, worker, sessionmaker, downloader

    await worker.stop()
    await engine.dispose()


async def _run_worker_once(worker: Worker, seconds: float = 0.3):
    async def _run():
        await worker.run()

    task = asyncio.create_task(_run())
    await asyncio.sleep(seconds)
    worker._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_http_to_download_lifecycle(app_env, tmp_path):
    """HTTP POST /v1/tasks → Worker 匿名下载 → 文件落盘 → 查询结果。"""
    fastapi_app, service, worker, sessionmaker, downloader = app_env

    # 建账号（save_and_download 需要）
    import asyncio

    async def _seed():
        async with session_scope(sessionmaker) as session:
            from panbridge.domain.models import Account

            await AccountRepository(session).add(
                Account(id="acc1", provider="mock", label="t")
            )

    asyncio.run(_seed())

    client = TestClient(fastapi_app)

    # 1. 通过 HTTP 创建任务（匿名下载）
    resp = client.post(
        "/v1/tasks",
        json={"input": {"text": "https://mock.local/s/mock001",
                        "action": "download"}},
    )
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["id"]
    assert resp.json()["status"] == "queued"

    # 2. Worker 处理任务
    asyncio.run(_run_worker_once(worker))

    # 3. 通过 HTTP 查询任务结果
    resp2 = client.get(f"/v1/tasks/{task_id}")
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["status"] == "completed", body
    # 递归下载根目录 f1/f2 + 子目录 f3/f4 = 4 个文件
    files = body["result"]["files"]
    assert len(files) == 4

    # 4. 文件已落盘（非空）
    import os

    for f in files:
        assert os.path.isfile(f["path"])
        assert os.path.getsize(f["path"]) > 0

    # 5. 通过 HTTP 获取任务文件列表
    resp3 = client.get(f"/v1/tasks/{task_id}/files")
    assert resp3.status_code == 200
    assert len(resp3.json()["items"]) == 4
