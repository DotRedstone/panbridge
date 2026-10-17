"""Worker 集成测试：完整任务生命周期。"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from panbridge.application.service import ApplicationService, CreateTaskInput
from panbridge.domain.enums import Action, TaskStatus
from panbridge.downloads.http import HttpDownloader
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


@pytest_asyncio.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)

    registry = ProviderRegistry()
    registry.register(MockProvider())

    downloader = HttpDownloader()
    service = ApplicationService(registry, sessionmaker, downloader=downloader)

    worker = Worker(
        registry,
        sessionmaker,
        downloader=downloader,
        download_dir=str(tmp_path / "dl"),
        poll_interval=0.01,
    )

    yield service, worker, sessionmaker
    await worker.stop()
    await engine.dispose()


async def _run_worker_once(worker: Worker, seconds: float = 0.2):
    """运行 worker 一小段时间处理队列中的任务。"""
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


@pytest.mark.asyncio
async def test_full_inspect_lifecycle(env):
    service, worker, _ = env
    task = await service.create_task(
        CreateTaskInput(
            text="https://mock.local/s/mock001",
            action=Action.INSPECT,
        )
    )
    await _run_worker_once(worker)
    result = await service.get_task(task.id)
    assert result.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_full_save_lifecycle(env):
    service, worker, sessionmaker = env
    # 先建账号
    async with session_scope(sessionmaker) as session:
        from panbridge.domain.models import Account

        acct = Account(id="acc1", provider="mock", label="t")
        await AccountRepository(session).add(acct)

    task = await service.create_task(
        CreateTaskInput(
            text="https://mock.local/s/mock001",
            action=Action.SAVE,
            account_id="acc1",
        )
    )
    await _run_worker_once(worker)
    result = await service.get_task(task.id)
    assert result.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_expired_account_fails(env):
    service, worker, sessionmaker = env
    async with session_scope(sessionmaker) as session:
        from panbridge.domain.models import Account

        acct = Account(id="expired1", provider="mock", label="t")
        await AccountRepository(session).add(acct)

    task = await service.create_task(
        CreateTaskInput(
            text="https://mock.local/s/mock001",
            action=Action.SAVE,
            account_id="expired1",
        )
    )
    await _run_worker_once(worker)
    result = await service.get_task(task.id)
    assert result.status == TaskStatus.FAILED
    assert result.error_code == "ACCOUNT_EXPIRED"


class _FakeDownloader:
    """记录下载调用，不真正访问网络。"""

    def __init__(self):
        self.calls: list[str] = []

    async def download(self, url, destination, **kwargs):
        self.calls.append(destination)
        yield {"downloaded": 0, "total": 0}


@pytest.mark.asyncio
async def test_save_and_download_lifecycle(env, tmp_path):
    service, worker, sessionmaker = env
    async with session_scope(sessionmaker) as session:
        from panbridge.domain.models import Account

        acct = Account(id="acc1", provider="mock", label="t")
        await AccountRepository(session).add(acct)

    fake = _FakeDownloader()
    worker.downloader = fake

    task = await service.create_task(
        CreateTaskInput(
            text="https://mock.local/s/mock001",
            action=Action.SAVE_AND_DOWNLOAD,
            account_id="acc1",
        )
    )
    await _run_worker_once(worker)
    result = await service.get_task(task.id)
    assert result.status == TaskStatus.COMPLETED
    # 转存了 2 个非目录文件，下载器应收到 2 次调用
    assert len(fake.calls) == 2


class _RecordingMockProvider(MockProvider):
    """记录 create_folder 调用，验证「未指定 destination 自动建文件夹」。"""

    def __init__(self):
        self.created_folders: list[tuple[str, str]] = []
        self.saved_destinations: list[str] = []

    async def create_folder(self, account, name, parent_id="0"):
        self.created_folders.append((name, parent_id))
        return f"dir-{name}"

    async def save_shared_files(
        self, account, share_id, password, file_ids, destination, on_conflict
    ):
        self.saved_destinations.append(destination or "")
        return await super().save_shared_files(
            account, share_id, password, file_ids, destination, on_conflict
        )


@pytest.mark.asyncio
async def test_save_auto_creates_folder_when_no_destination(tmp_path):
    """未指定 destination 时，Worker 应自动创建独立文件夹再转存。"""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)

    provider = _RecordingMockProvider()
    registry = ProviderRegistry()
    registry.register(provider)

    downloader = _FakeDownloader()
    service = ApplicationService(registry, sessionmaker, downloader=downloader)
    worker = Worker(
        registry,
        sessionmaker,
        downloader=downloader,
        download_dir=str(tmp_path / "dl"),
        poll_interval=0.01,
    )

    async with session_scope(sessionmaker) as session:
        from panbridge.domain.models import Account

        await AccountRepository(session).add(
            Account(id="acc1", provider="mock", label="t")
        )

    task = await service.create_task(
        CreateTaskInput(
            text="https://mock.local/s/mock001",
            action=Action.SAVE_AND_DOWNLOAD,
            account_id="acc1",
            # 不指定 destination
        )
    )
    await _run_worker_once(worker)
    result = await service.get_task(task.id)
    assert result.status == TaskStatus.COMPLETED

    # 应创建了文件夹，且转存用了该文件夹 fid（而非根目录 "0"）
    assert len(provider.created_folders) == 1
    assert provider.saved_destinations[0].startswith("dir-panbridge-")

    await worker.stop()
    await engine.dispose()


def test_local_conflict_rename(tmp_path):
    """on_conflict=rename 时，本地已存在同名文件应自动改名不覆盖。"""
    import os

    worker = Worker(
        ProviderRegistry(),
        None,
        download_dir=str(tmp_path / "dl"),
    )
    name = "video.mkv"
    download_dir = tmp_path / "dl"
    download_dir.mkdir(parents=True, exist_ok=True)
    existing = download_dir / name
    existing.write_text("x")

    resolved = worker._resolve_local_path(name, "rename")
    assert resolved != str(existing)
    assert os.path.basename(resolved).startswith("video")
    assert "(1)" in os.path.basename(resolved)

    # overwrite 默认返回原路径
    assert worker._resolve_local_path(name, "overwrite") == str(existing)
