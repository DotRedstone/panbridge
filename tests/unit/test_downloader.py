"""HTTP 下载器单元测试（mock httpx，不访问真实网络）。"""

from __future__ import annotations

import asyncio
import os

from panbridge.downloads.http import HttpDownloader


class _FakeStream:
    def __init__(self, chunks: list[bytes], status: int = 200, headers=None):
        self._chunks = chunks
        self.status_code = status
        self.headers = headers or {"content-length": str(sum(len(c) for c in chunks))}

    async def aiter_bytes(self, chunk_size):
        for c in self._chunks:
            yield c

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")


class _FakeResponse:
    def __init__(self, stream: _FakeStream):
        self._stream = stream

    async def __aenter__(self):
        return self._stream

    async def __aexit__(self, *args):
        return False


class _FakeClient:
    def __init__(self, stream: _FakeStream):
        self._stream = stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, method, url, headers=None):
        return _FakeResponse(self._stream)


def test_http_downloader_writes_file(tmp_path, monkeypatch):
    import panbridge.downloads.http as http_mod

    fake = _FakeClient(_FakeStream([b"hello ", b"world"]))

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return fake

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(http_mod.httpx, "AsyncClient", _FakeAsyncClient)

    dest = str(tmp_path / "out.bin")
    downloader = HttpDownloader()

    async def run():
        progress = []
        async for p in downloader.download(
            "https://download.example.com/f.bin", dest
        ):
            progress.append(p)
        return progress

    progress = asyncio.run(run())

    assert os.path.exists(dest)
    with open(dest, "rb") as f:
        assert f.read() == b"hello world"
    # 无残留临时文件
    assert not os.path.exists(dest + ".part")
    # 最后一条进度表示完成
    assert progress[-1].downloaded == progress[-1].total == 11
