"""AstrBot 插件（astrbot-plugin/）单元测试。

- PanBridgeClient：用 httpx MockTransport 模拟服务端响应，验证请求路径/方法/鉴权头。
- main.py 纯函数：_parse_args / _format_inspect / _format_task / _fmt_size。
（main.py 顶部 import astrbot，测试仅 import 纯函数，不触发 Star 类加载。）
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "astrbot-plugin"
sys.path.insert(0, str(PLUGIN_DIR))

from panbridge_client import PanBridgeClient, PanBridgeError  # noqa: E402

# ----------------------------------------------------------------------
# PanBridgeClient
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_posts_correctly():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/shares/inspect"
        body = request.content.decode("utf-8")
        assert "pan.quark.cn" in body
        return httpx.Response(200, json={"provider": "quark", "files": []})

    client = PanBridgeClient("http://127.0.0.1:8000")
    client._transport = httpx.MockTransport(handler)
    result = await client.inspect("https://pan.quark.cn/s/abc")
    assert result["provider"] == "quark"


@pytest.mark.asyncio
async def test_api_key_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-API-Key") == "secret123"
        return httpx.Response(200, json={"providers": []})

    client = PanBridgeClient("http://127.0.0.1:8000", api_key="secret123")
    client._transport = httpx.MockTransport(handler)
    await client.list_providers()


@pytest.mark.asyncio
async def test_error_maps_panbridge_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"code": "INVALID_SHARE", "message": "share not found"}
        )

    client = PanBridgeClient("http://127.0.0.1:8000")
    client._transport = httpx.MockTransport(handler)
    with pytest.raises(PanBridgeError) as exc:
        await client.inspect("https://bad")
    assert exc.value.code == "INVALID_SHARE"
    assert "share not found" in exc.value.message


@pytest.mark.asyncio
async def test_create_task_builds_input():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(202, json={"id": "t1", "status": "queued"})

    client = PanBridgeClient("http://127.0.0.1:8000")
    client._transport = httpx.MockTransport(handler)
    task = await client.create_task(
        "https://pan.quark.cn/s/abc",
        action="download",
        selection=["f1"],
        destination="/dl",
    )
    assert task["id"] == "t1"
    assert "download" in captured["body"]
    assert "f1" in captured["body"]


@pytest.mark.asyncio
async def test_start_qr_login_passes_provider():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = str(request.url)
        return httpx.Response(200, json={"session_id": "s1", "qr_url": "http://qr"})

    client = PanBridgeClient("http://127.0.0.1:8000")
    client._transport = httpx.MockTransport(handler)
    result = await client.start_qr_login("uc")
    assert result["session_id"] == "s1"
    assert "provider=uc" in captured["query"]


# ----------------------------------------------------------------------
# utils.py 纯函数（不依赖 astrbot）
# ----------------------------------------------------------------------
def test_parse_args_strips_prefix():
    from utils import parse_args

    assert parse_args("/pb download https://x.com/a") == ["download", "https://x.com/a"]
    assert parse_args("pb save https://x.com/a") == ["save", "https://x.com/a"]
    assert parse_args("/pb") == []


def test_parse_args_handles_quoted_password():
    from utils import parse_args

    assert parse_args('/pb https://x.com/a "提取码 123"') == [
        "https://x.com/a",
        "提取码 123",
    ]


def test_format_inspect():
    from utils import format_inspect

    info = {
        "provider": "quark",
        "title": "t",
        "files": [
            {"name": "a.mkv", "is_dir": False, "size": 1048576},
            {"name": "dir", "is_dir": True, "size": None},
        ],
    }
    out = format_inspect(info)
    assert "quark" in out
    assert "a.mkv" in out
    assert "1.0MB" in out
    assert "dir/" in out


def test_format_task_with_error():
    from utils import format_task

    task = {
        "id": "t1",
        "action": "download",
        "status": "failed",
        "error_message": "boom",
    }
    out = format_task(task)
    assert "failed" in out
    assert "boom" in out


def test_fmt_size():
    from utils import fmt_size

    assert "1.0KB" in fmt_size(1024)
    assert "1.0MB" in fmt_size(1048576)
    assert fmt_size(None) == ""
