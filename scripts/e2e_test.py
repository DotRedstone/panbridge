"""端到端验证脚本（Mock Provider）。

在不依赖真实网盘账号的情况下，验证完整业务链路：
启动服务 → 注入密钥 → 添加账号（凭据加密）→ inspect → save_and_download → 查询结果。

用法：
    PANBRIDGE_SECRET_KEY=test-key PYTHONPATH=src python scripts/e2e_test.py

注意：本脚本用 Mock Provider 验证链路正确性，不访问真实网盘。
真实夸克验证需按 docs/providers.md 的「人工验证步骤」操作。
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from panbridge.application.service import ApplicationService, CreateTaskInput
from panbridge.domain.enums import Action, TaskStatus
from panbridge.domain.models import Account
from panbridge.infrastructure.crypto import CredentialCipher
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


class _NoopDownloader:
    """记录下载调用，不真正写文件（避免 Mock URL 被 SSRF 拦截）。"""

    def __init__(self):
        self.calls: list[str] = []

    async def download(self, url, destination, **kwargs):
        self.calls.append(destination)
        yield {"downloaded": 0, "total": 0}


async def main() -> None:
    secret = os.environ.get("PANBRIDGE_SECRET_KEY", "")
    if not secret:
        print("[错误] 请设置 PANBRIDGE_SECRET_KEY（任意非空字符串即可）")
        return

    tmpdir = tempfile.mkdtemp(prefix="panbridge-e2e-")
    engine = create_engine(f"sqlite+aiosqlite:///{tmpdir}/test.db")
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)

    registry = ProviderRegistry()
    registry.register(MockProvider())

    downloader = _NoopDownloader()
    service = ApplicationService(registry, sessionmaker, downloader=downloader)
    worker = Worker(
        registry,
        sessionmaker,
        downloader=downloader,
        download_dir=os.path.join(tmpdir, "dl"),
        poll_interval=0.01,
    )

    # 1. 用密钥加密凭据，模拟「API 层加密存储」
    cipher = CredentialCipher(secret)
    encrypted_cookie = cipher.encrypt("mock-cookie-value")

    async with session_scope(sessionmaker) as session:
        await AccountRepository(session).add(
            Account(
                id="acc1",
                provider="mock",
                label="e2e",
                encrypted_credential=encrypted_cookie,
            )
        )

    # 2. 启动 worker
    worker_task = asyncio.create_task(worker.run())

    # 3. inspect
    inspect = await service.inspect_share(
        "https://mock.local/s/mock001 提取码: 1234"
    )
    print(f"[inspect] provider={inspect['provider']} files={len(inspect['files'])}")

    # 4. save_and_download
    task = await service.create_task(
        CreateTaskInput(
            text="https://mock.local/s/mock001",
            action=Action.SAVE_AND_DOWNLOAD,
            account_id="acc1",
        )
    )
    print(f"[create] task_id={task.id} status={task.status.value}")

    # 5. 等待 worker 处理
    for _ in range(50):
        await asyncio.sleep(0.1)
        result = await service.get_task(task.id)
        if result.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            break

    print(f"[result] status={result.status.value} error={result.error_code}")
    print(f"[downloads] {len(downloader.calls)} files saved")

    worker._running = False
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    await engine.dispose()

    # 断言
    assert result.status == TaskStatus.COMPLETED, "任务应完成"
    assert len(downloader.calls) == 2, "应下载 2 个非目录文件"
    print("\n✅ 端到端验证通过（Mock Provider）")


if __name__ == "__main__":
    asyncio.run(main())
