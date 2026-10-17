"""任务执行 Worker。

独立的后台循环，从数据库租约领取任务并执行完整业务链路。
使用租约 + 心跳机制保证单 Worker 可靠执行，
支持异常重启后的任务恢复。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from panbridge.domain.enums import Action, TaskStatus
from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Task
from panbridge.persistence import TaskRepository, session_scope
from panbridge.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        registry: ProviderRegistry,
        sessionmaker,
        *,
        worker_id: str = "worker-1",
        lease_seconds: int = 300,
        max_attempts: int = 3,
        poll_interval: float = 1.0,
        downloader=None,
        download_dir: str = "./downloads",
    ) -> None:
        self.registry = registry
        self.sessionmaker = sessionmaker
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.poll_interval = poll_interval
        self.downloader = downloader
        self.download_dir = download_dir
        self._running = False

    async def run(self) -> None:
        """主循环：领取并执行任务，直到被停止。"""
        self._running = True
        while self._running:
            try:
                task = await self._claim()
                if task is None:
                    await asyncio.sleep(self.poll_interval)
                    continue
                await self._execute(task)
            except asyncio.CancelledError:
                logger.info("worker %s cancelled", self.worker_id)
                raise
            except Exception:
                logger.exception("worker loop error")

    async def _claim(self) -> Task | None:
        async with session_scope(self.sessionmaker) as session:
            return await TaskRepository(session).claim_next(
                self.worker_id, self.lease_seconds
            )

    async def _execute(self, task: Task) -> None:
        logger.info("executing task %s action=%s", task.id, task.action.value)
        try:
            provider = self.registry.get(task.provider or "")
            result = await self._run_task(task, provider)
            await self._complete(task.id, result)
        except DomainError as exc:
            await self._fail(task.id, exc)
        except Exception:  # noqa: BLE001
            logger.exception("unexpected error in task %s", task.id)
            await self._fail(
                task.id,
                DomainError(
                    ErrorCode.INTERNAL_ERROR,
                    "internal error",
                    retryable=True,
                ),
            )

    async def _run_task(self, task: Task, provider) -> dict:
        """执行具体业务动作，返回结果字典。"""
        # 解析分享
        from panbridge.application.link_extraction import extract_share

        extracted = extract_share(task.input_text or "")
        share = await provider.inspect_share(
            extracted.share_id, extracted.password
        )

        if task.action == Action.INSPECT:
            return {"title": share.title, "files": len(share.files)}

        # 选择要转存的文件（默认全部非目录文件）
        selected = task.selection or [
            f.file_id for f in share.files if not f.is_dir
        ]
        # 源文件名映射（下载时用于定位转存后的文件）
        name_by_fid = {f.file_id: f.name for f in share.files}

        account = None
        saved_file_ids: list[str] = []

        if task.action in (Action.SAVE, Action.SAVE_AND_DOWNLOAD):
            account = await self._get_account(task.account_id)
            await self._set_status(task.id, TaskStatus.SAVING)
            transfer = await provider.save_shared_files(
                account,
                extracted.share_id,
                extracted.password,
                selected,
                task.destination,
                task.on_conflict.value,
            )
            saved_file_ids = transfer.saved_file_ids
            if task.action == Action.SAVE:
                return {"saved": saved_file_ids}

        if task.action in (Action.DOWNLOAD, Action.SAVE_AND_DOWNLOAD):
            if self.downloader is None:
                raise DomainError(
                    ErrorCode.UNSUPPORTED_OPERATION,
                    "download backend not configured",
                )
            account = account or await self._get_account(task.account_id)
            await self._set_status(task.id, TaskStatus.DOWNLOADING)

            # 下载源文件 ID：纯 download 用源分享的 fid；
            # save_and_download 需用转存后的新 fid（若平台返回空则查询）。
            download_file_ids = selected
            if task.action == Action.SAVE_AND_DOWNLOAD:
                if saved_file_ids:
                    download_file_ids = saved_file_ids
                else:
                    names = [name_by_fid.get(f, f) for f in selected]
                    resolved = await provider.resolve_saved_file_ids(
                        account, task.destination, names
                    )
                    download_file_ids = resolved or selected

            downloaded = await self._download_files(
                provider, account, download_file_ids, name_by_fid
            )
            return {"downloaded": downloaded}

        return {}

    async def _get_account(self, account_id: str | None):
        from panbridge.persistence import AccountRepository

        if account_id is None:
            raise DomainError(
                ErrorCode.ACCOUNT_NOT_FOUND,
                "account_id is required for save/download",
            )
        async with session_scope(self.sessionmaker) as session:
            account = await AccountRepository(session).get(account_id)
            if account is None:
                raise DomainError(
                    ErrorCode.ACCOUNT_NOT_FOUND,
                    f"account not found: {account_id}",
                )
            return account

    async def _download_files(
        self, provider, account, file_ids: list[str], name_by_fid: dict
    ) -> list[str]:
        results = []
        for fid in file_ids:
            info = await provider.get_download_info(account, fid)
            url = info["url"]
            # 优先用原始文件名，避免用 fid 作为文件名
            name = name_by_fid.get(fid) or info.get("name") or f"{fid}.bin"
            dest = f"{self.download_dir}/{name}"
            headers = info.get("headers") or {}
            async for _progress in self.downloader.download(
                url, dest, headers=headers
            ):
                pass
            results.append(dest)
        return results

    async def _set_status(self, task_id: str, status: TaskStatus) -> None:
        async with session_scope(self.sessionmaker) as session:
            repo = TaskRepository(session)
            task = await repo.get(task_id)
            if task and task.status != TaskStatus.CANCELLED:
                task.status = status
                await repo.update(task)

    async def _complete(self, task_id: str, result: dict) -> None:
        async with session_scope(self.sessionmaker) as session:
            repo = TaskRepository(session)
            task = await repo.get(task_id)
            if task is None:
                return
            if task.status == TaskStatus.CANCELLED:
                return
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now(UTC)
            task.lease_owner = None
            task.lease_expires_at = None
            await repo.update(task)

    async def _fail(self, task_id: str, exc: DomainError) -> None:
        async with session_scope(self.sessionmaker) as session:
            repo = TaskRepository(session)
            task = await repo.get(task_id)
            if task is None or task.status == TaskStatus.CANCELLED:
                return
            if exc.retryable and task.attempt < task.max_attempts:
                task.status = TaskStatus.RETRYING
                # 归还队列
                task.status = TaskStatus.QUEUED
            else:
                task.status = TaskStatus.FAILED
                task.completed_at = datetime.now(UTC)
            task.error_code = exc.code.value
            task.error_message = exc.message
            task.retryable = exc.retryable
            task.lease_owner = None
            task.lease_expires_at = None
            await repo.update(task)

    async def stop(self) -> None:
        self._running = False
