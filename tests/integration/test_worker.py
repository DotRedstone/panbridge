"""Worker 集成测试：完整任务生命周期。"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from panbridge.application.service import ApplicationService, CreateTaskInput
from panbridge.domain.enums import Action, TaskStatus
from panbridge.domain.models import Account, ShareFile
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


class _AnonDownloadProvider(MockProvider):
    """模拟支持匿名分享直链下载的平台（如蓝奏云/123/PikPak）。

    get_share_download_info 返回直链与文件名，无需账号即可下载。
    """

    async def get_share_download_info(self, share_id, password, file_id, file=None):
        return {
            "url": f"https://download.mock.local/{file_id}",
            "name": f"{file_id}.bin",
            "size": 1024,
            "checksum": None,
            "headers": {},
        }


@pytest.mark.asyncio
async def test_anonymous_download_lifecycle(tmp_path):
    """匿名下载：无账号也能通过 get_share_download_info 落盘文件。"""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)

    registry = ProviderRegistry()
    registry.register(_AnonDownloadProvider())

    downloader = _FakeDownloader()
    service = ApplicationService(registry, sessionmaker, downloader=downloader)
    worker = Worker(
        registry,
        sessionmaker,
        downloader=downloader,
        download_dir=str(tmp_path / "dl"),
        poll_interval=0.01,
    )

    # 不指定 account_id：纯匿名下载
    task = await service.create_task(
        CreateTaskInput(
            text="https://mock.local/s/mock001",
            action=Action.DOWNLOAD,
        )
    )
    await _run_worker_once(worker)
    result = await service.get_task(task.id)
    assert result.status == TaskStatus.COMPLETED
    # 递归下载根目录 f1/f2 + 子目录 f3/f4 = 4 个文件
    assert len(downloader.calls) == 4
    # 文件名应来自 get_share_download_info，而非 fid 裸名
    names = [f["name"] for f in result.result["files"]]
    assert "mock001-f1.bin" in names

    await worker.stop()
    await engine.dispose()


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
    # 递归转存并下载：根目录 f1/f2 + 子目录 f3/f4 = 4 个文件
    assert len(fake.calls) == 4


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

    # 应创建了独立根文件夹 + 两个子目录（保留目录结构）。
    # 根文件直接转存到根文件夹，不新建额外子目录。
    assert len(provider.created_folders) == 3
    # 第一个是自动创建的根文件夹（panbridge-<taskid前8>）
    assert provider.created_folders[0][0].startswith("panbridge-")
    assert provider.created_folders[0][1] == "0"
    # 后两个是分享中的子目录 folder / sub，父级为对应目录 fid
    assert provider.created_folders[1][0] == "folder"
    assert provider.created_folders[2][0] == "sub"
    # 转存 destination 不应是根目录 "0"，而应是自动创建的文件夹 fid
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


class _IncompleteResolveProvider(MockProvider):
    """模拟转存异步、resolve 返回数量不足的平台（应如实报错，不伪造成功）。"""

    async def save_shared_files(
        self, account, share_id, password, file_ids, destination, on_conflict
    ):
        # 转存返回空 saved_file_ids（异步任务，需 resolve 兜底）
        from panbridge.domain.models import TransferResult

        return TransferResult(saved_file_ids=[], remote_task_id="mock-task")

    async def resolve_saved_file_ids(self, account, destination, names):
        # 只返回 1 个新 fid，但应有 2 个文件 → 数量不足
        return ["new-fid-1"]


@pytest.mark.asyncio
async def test_save_and_download_incomplete_resolve_fails(tmp_path):
    """转存后 resolve 返回的新 fid 数量不足时，应如实报错而非伪造成功。"""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)

    registry = ProviderRegistry()
    registry.register(_IncompleteResolveProvider())

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
        )
    )
    await _run_worker_once(worker)
    result = await service.get_task(task.id)
    # resolve 数量不足 → 任务失败，而非伪装成功
    assert result.status == TaskStatus.FAILED
    assert result.error_code == "DOWNLOAD_FAILED"

    await worker.stop()
    await engine.dispose()


class _RecordingDownloader:
    """记录下载的 destination 路径，模拟真实落盘。"""

    def __init__(self):
        self.destinations: list[str] = []

    async def download(self, url, destination, **kwargs):
        self.destinations.append(destination)
        # 模拟实际创建文件
        import os

        os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
        with open(destination, "wb") as f:
            f.write(b"x")
        yield {"downloaded": 1, "total": 1}


@pytest.mark.asyncio
async def test_recursive_directory_download_preserves_structure(tmp_path):
    """多级目录分享应完整下载并保留目录结构。"""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)

    registry = ProviderRegistry()
    registry.register(_AnonDownloadProvider())

    downloader = _RecordingDownloader()
    service = ApplicationService(registry, sessionmaker, downloader=downloader)
    worker = Worker(
        registry,
        sessionmaker,
        downloader=downloader,
        download_dir=str(tmp_path / "dl"),
        poll_interval=0.01,
    )

    task = await service.create_task(
        CreateTaskInput(
            text="https://mock.local/s/mock001",
            action=Action.DOWNLOAD,
        )
    )
    await _run_worker_once(worker)
    result = await service.get_task(task.id)
    assert result.status == TaskStatus.COMPLETED

    # 根目录 f1/f2 + 子目录 d1/f3 + 孙目录 d1a/f4 = 4 个文件
    files = result.result["files"]
    assert len(files) == 4

    # 相对路径应保留目录结构
    rel_paths = {f["rel_path"] for f in files}
    assert "video.mkv" in rel_paths
    assert "subtitle.srt" in rel_paths
    assert "folder/deep.txt" in rel_paths
    assert "folder/sub/deepest.bin" in rel_paths

    # 落盘路径应在 download_dir 下保留结构
    import os

    assert os.path.isfile(
        os.path.join(worker.download_dir, "folder", "deep.txt")
    )
    assert os.path.isfile(
        os.path.join(worker.download_dir, "folder", "sub", "deepest.bin")
    )

    await worker.stop()
    await engine.dispose()


class _SameNameProvider(MockProvider):
    """不同目录下有同名文件的分享（用于验证转存不冲突、映射不混淆）。

    结构：
      root/readme.txt        <- 根目录同名文件
      folder/readme.txt      <- 与根目录同名
    """

    async def list_share_files(
        self, share_id, password, parent_id=None
    ):
        prefix = share_id
        if parent_id == f"{prefix}-d1":
            return [
                ShareFile(
                    file_id=f"{prefix}-f3", name="readme.txt", size=32,
                    parent_id=f"{prefix}-d1",
                ),
            ]
        return await super().list_share_files(share_id, password, parent_id)

    async def inspect_share(self, share_id, password):
        share = await super().inspect_share(share_id, password)
        # 根目录额外加一个 readme.txt，与子目录同名
        share.files = [
            ShareFile(
                file_id=f"{share_id}-f0", name="readme.txt", size=16,
            ),
            *share.files,
        ]
        return share


@pytest.mark.asyncio
async def test_save_and_download_preserves_structure_and_names(tmp_path):
    """目录感知转存：不同目录同名文件不应冲突，映射按目录严格对应。"""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)

    provider = _SameNameProvider()
    registry = ProviderRegistry()
    registry.register(provider)

    downloader = _RecordingDownloader()
    service = ApplicationService(registry, sessionmaker, downloader=downloader)
    worker = Worker(
        registry,
        sessionmaker,
        downloader=downloader,
        download_dir=str(tmp_path / "dl"),
        poll_interval=0.01,
    )

    async with session_scope(sessionmaker) as session:
        await AccountRepository(session).add(
            Account(id="acc1", provider="mock", label="t")
        )

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

    # 根目录 readme.txt/video.mkv/subtitle.srt + folder/readme.txt = 4 个文件
    files = result.result["files"]
    rel_paths = {f["rel_path"] for f in files}
    assert "readme.txt" in rel_paths
    assert "folder/readme.txt" in rel_paths
    assert len(files) == 4

    await worker.stop()
    await engine.dispose()


class _WrongOrderProvider(MockProvider):
    """返回 saved_file_ids 数量正确但顺序错乱（模拟平台不保证顺序）。"""

    async def save_shared_files(
        self, account, share_id, password, file_ids, destination, on_conflict
    ):
        # 故意打乱顺序：数量一致但位置错乱
        from panbridge.domain.models import TransferResult

        saved = [f"saved-{fid}" for fid in reversed(file_ids)]
        return TransferResult(saved_file_ids=saved)


@pytest.mark.asyncio
async def test_transfer_count_mismatch_fails_not_forged(tmp_path):
    """转存返回 fid 数量与源文件数不一致时，应如实报错而非伪造成功。"""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)

    provider = _IncompleteResolveProvider()
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
        await AccountRepository(session).add(
            Account(id="acc1", provider="mock", label="t")
        )

    task = await service.create_task(
        CreateTaskInput(
            text="https://mock.local/s/mock001",
            action=Action.SAVE_AND_DOWNLOAD,
            account_id="acc1",
        )
    )
    await _run_worker_once(worker)
    result = await service.get_task(task.id)
    assert result.status == TaskStatus.FAILED
    assert result.error_code == "DOWNLOAD_FAILED"

    await worker.stop()
    await engine.dispose()
