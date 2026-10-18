"""本地 HTTP 服务器集成测试：用真实文件字节验证下载闭环。

区别于 Mock Provider 返回值测试，本文件启动真实本地 HTTP 服务器，
返回真实字节，覆盖：
- 完整下载 + 目录结构保留
- 断线（服务器提前关闭连接 / 内容长度不符）
- 部分下载（截断）导致的大小校验失败
- 下载量上限（quota）
- 重试语义

`HttpDownloader(allow_loopback=True)` 仅在测试中开启回环地址下载，
生产环境默认关闭（validate_download_url 拒绝 127.0.0.1）。
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.downloads.http import HttpDownloader

# 真实可下载的字节内容（每种路径固定内容，用于校验和比对）
_CONTENT = {
    "video.mkv": b"FAKE-MKV-" * 200,          # 1800 字节
    "subtitle.srt": b"1\n00:00:00,000 --> 00:00:01,000\nhello\n",
    "folder/deep.txt": b"deep file content\n" * 30,
    "folder/sub/deepest.bin": bytes(range(256)),
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Handler(BaseHTTPRequestHandler):
    """根据路径返回真实字节；支持模拟断线/截断/超限。"""

    def do_GET(self):  # noqa: N802
        path = self.path.lstrip("/")
        # 特殊路径模拟行为
        if path == "disconnect":
            # 声明较大长度但立即断开 → 触发连接重置/截断
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.write(b"only-a-few-bytes")
            self.wfile.flush()
            return
        if path == "truncate":
            data = _CONTENT["video.mkv"]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            # 只写一半 → 内容长度不符
            self.wfile.write(data[: len(data) // 2])
            self.wfile.flush()
            return
        if path == "huge":
            # 10MB 超限文件（配合 max_size 测试）
            self.send_response(200)
            self.send_header("Content-Length", str(10 * 1024 * 1024))
            self.end_headers()
            try:
                chunk = b"x" * (64 * 1024)
                for _ in range(160):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            return

        data = _CONTENT.get(path)
        if data is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def log_message(self, *args):  # noqa: A003
        pass


@pytest_asyncio.fixture
async def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.asyncio
async def test_download_real_bytes_full(http_server, tmp_path):
    """真实字节完整下载：内容与大小一致。"""
    downloader = HttpDownloader(allow_loopback=True)
    dest = str(tmp_path / "video.mkv")
    async for _ in downloader.download(f"{http_server}/video.mkv", dest):
        pass
    data = open(dest, "rb").read()  # noqa: SIM115
    assert data == _CONTENT["video.mkv"]


@pytest.mark.asyncio
async def test_download_disconnect_raises_retryable(http_server, tmp_path):
    """断线（声明长度与实际不符）应抛可重试错误。"""
    downloader = HttpDownloader(allow_loopback=True)
    dest = str(tmp_path / "disconnect.bin")
    with pytest.raises(DomainError) as exc:
        async for _ in downloader.download(f"{http_server}/disconnect", dest):
            pass
    # httpx 会因连接提前关闭/长度不符抛 HTTPError → 映射为 retryable
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_download_truncate_partial_file(http_server, tmp_path):
    """部分下载（服务器截断）应留下 .part 临时文件，不产出最终文件。"""
    downloader = HttpDownloader(allow_loopback=True)
    dest = str(tmp_path / "truncated.mkv")
    with pytest.raises(DomainError):
        async for _ in downloader.download(f"{http_server}/truncate", dest):
            pass
    # 最终文件不应存在（截断未完成）
    import os

    assert not os.path.exists(dest)


@pytest.mark.asyncio
async def test_download_max_size_limit(http_server, tmp_path):
    """下载量上限：超过 max_size 应中止并抛错。"""
    downloader = HttpDownloader(allow_loopback=True, max_size=1024)
    dest = str(tmp_path / "huge.bin")
    with pytest.raises(DomainError) as exc:
        async for _ in downloader.download(f"{http_server}/huge", dest):
            pass
    assert exc.value.code == ErrorCode.DOWNLOAD_FAILED


@pytest.mark.asyncio
async def test_download_checksum_verification(tmp_path):
    """校验和校验：Worker 层对真实字节做 sha256 校验。"""
    from panbridge.workers.worker import Worker

    dest = str(tmp_path / "f.bin")
    data = _CONTENT["subtitle.srt"]
    open(dest, "wb").write(data)  # noqa: SIM115

    # 正确校验和 → 不抛错
    Worker._verify_download("f.bin", dest, len(data), f"sha256:{_sha256(data)}")

    # 错误校验和 → 抛 DOWNLOAD_FAILED
    with pytest.raises(DomainError) as exc:
        Worker._verify_download("f.bin", dest, len(data), "sha256:" + "0" * 64)
    assert exc.value.code == ErrorCode.DOWNLOAD_FAILED


@pytest.mark.asyncio
async def test_download_size_mismatch_fails(tmp_path):
    """大小不符应抛错（不当作成功落盘）。"""
    from panbridge.workers.worker import Worker

    dest = str(tmp_path / "f.bin")
    open(dest, "wb").write(b"short")  # noqa: SIM115

    with pytest.raises(DomainError) as exc:
        Worker._verify_download("f.bin", dest, expected_size=100, checksum=None)
    assert exc.value.code == ErrorCode.DOWNLOAD_FAILED
    assert exc.value.retryable is True


class _LocalHttpAnonProvider:
    """指向本地 HTTP 服务器的匿名分享 Provider（真实字节下载）。

    provider 名取 `mock`，使 extract_share 把 `https://mock.local/s/x`
    识别为 mock 平台，从而复用现有的链接识别逻辑。
    """

    name = "mock"
    display_name = "Local HTTP"
    capabilities = frozenset(
        {
            "inspect_share",
            "list_share_files",
            "get_share_download_info",
            "validate_account",
        }
    )

    def __init__(self, base_url: str):
        self._base = base_url

    async def inspect_share(self, share_id, password):
        from panbridge.domain.models import Share, ShareFile

        return Share(
            provider=self.name,
            share_id=share_id,
            title="local-http-share",
            files=[
                ShareFile(file_id="video.mkv", name="video.mkv", size=1800),
                ShareFile(file_id="subtitle.srt", name="subtitle.srt", size=48),
                ShareFile(file_id="folder", name="folder", is_dir=True),
            ],
        )

    async def list_share_files(self, share_id, password, parent_id=None):
        from panbridge.domain.models import ShareFile

        if parent_id == "folder":
            return [
                ShareFile(
                    file_id="folder/deep.txt", name="deep.txt", size=510,
                    parent_id="folder",
                ),
            ]
        return (await self.inspect_share(share_id, password)).files

    async def validate_account(self, account):
        return True

    async def get_share_download_info(self, share_id, password, file_id, file=None):
        data = _CONTENT[file_id]
        return {
            "url": f"{self._base}/{file_id}",
            "name": file_id.rsplit("/", 1)[-1],
            "size": len(data),
            "checksum": f"sha256:{_sha256(data)}",
            "headers": {},
        }


@pytest.mark.asyncio
async def test_worker_downloads_real_bytes_preserving_structure(
    http_server, tmp_path
):
    """端到端：Worker 经本地 HTTP 服务器下载真实字节并保留目录结构。"""
    from panbridge.application.service import ApplicationService, CreateTaskInput
    from panbridge.domain.enums import Action, TaskStatus
    from panbridge.persistence import (
        create_engine,
        create_sessionmaker,
        init_db,
    )
    from panbridge.providers.registry import ProviderRegistry
    from panbridge.workers.worker import Worker

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)

    provider = _LocalHttpAnonProvider(http_server)
    registry = ProviderRegistry()
    registry.register(provider)

    downloader = HttpDownloader(allow_loopback=True)
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
            text="https://mock.local/s/localhttp",
            action=Action.DOWNLOAD,
        )
    )
    # 运行 worker 一小段时间
    async def _run():
        await worker.run()

    worker_t = asyncio.create_task(_run())
    await asyncio.sleep(0.3)
    worker._running = False
    worker_t.cancel()
    try:
        await worker_t
    except asyncio.CancelledError:
        pass

    result = await service.get_task(task.id)
    assert result.status == TaskStatus.COMPLETED, result.error_message

    files = result.result["files"]
    rel_paths = {f["rel_path"] for f in files}
    assert "video.mkv" in rel_paths
    assert "subtitle.srt" in rel_paths
    assert "folder/deep.txt" in rel_paths

    # 真实字节落盘并保留目录结构
    import os

    assert os.path.isfile(os.path.join(worker.download_dir, "video.mkv"))
    assert os.path.isfile(
        os.path.join(worker.download_dir, "folder", "deep.txt")
    )
    with open(os.path.join(worker.download_dir, "video.mkv"), "rb") as f:
        assert f.read() == _CONTENT["video.mkv"]

    await worker.stop()
    await engine.dispose()

