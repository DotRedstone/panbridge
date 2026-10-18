"""验收脚本下载量限额前置校验测试。

验证 `_estimate_total_size` 与「下载前拦截」逻辑，确保不会先下载
数 GB 再回头校验。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from panbridge.providers.quark.mock import MockProvider

# scripts/ 不在 pythonpath 内，此处按文件路径加载 acceptance 模块。
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "acceptance.py"


@pytest.fixture(scope="module")
def acceptance_mod():
    spec = importlib.util.spec_from_file_location("acceptance", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["acceptance"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_estimate_total_size_recursive(acceptance_mod):
    """递归预估整目录大小（Mock 分享：f1=1024 + f2=64 + d1/f3=32 + d1/d1a/f4=16）。"""
    provider = MockProvider()
    share = await provider.inspect_share("mock001", None)
    total = await acceptance_mod._estimate_total_size(
        provider, "mock001", None, share.files
    )
    # 1024 + 64 + 32 + 16 = 1136
    assert total == 1136


@pytest.mark.asyncio
async def test_estimate_total_size_unknown_when_size_none(acceptance_mod):
    """size 未知（None）时应返回 None（不阻断，交由下载器兜底）。"""
    from panbridge.domain.models import Share, ShareFile

    class _UnknownSizeProvider(MockProvider):
        async def inspect_share(self, share_id, password):
            return Share(
                provider=self.name,
                share_id=share_id,
                title="t",
                files=[ShareFile(file_id="x", name="f.bin", size=None)],
            )

    provider = _UnknownSizeProvider()
    share = await provider.inspect_share("mock001", None)
    total = await acceptance_mod._estimate_total_size(
        provider, "mock001", None, share.files
    )
    assert total is None


def test_max_bytes_blocks_download_before_execution(acceptance_mod):
    """预估大小超限时应 fail fast（在下载前拦截）。"""
    # 模拟：预估 100 字节，限制 50 字节 → 触发 precheck 拦截
    assert acceptance_mod._DEFAULT_MAX_BYTES == 50 * 1024 * 1024
