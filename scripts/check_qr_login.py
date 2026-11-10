"""扫码登录协议连通性检查。

只调用各平台扫码登录 backend 的 `start()`（生成二维码），**不做**后续的
check/finish（那需要真实手机扫码）。用于快速回归验证「扫码登录协议第一步
是否还活着」——若某平台二维码生成失败，说明其协议端点已变更，需要更新
对应 `qr_backend.py`。

用法：
    python scripts/check_qr_login.py              # 检查全部 5 个平台
    python scripts/check_qr_login.py --provider quark   # 只查单个平台

输出：每个平台一行 `OK / FAIL / SKIP`，OK 时打印二维码 URL 前缀。
退出码：全部 OK 为 0，任一 FAIL 为 1。

前置：已按 README 装好依赖（quarkpan / p115client / httpx），
并在能访问网盘域名的网络环境下运行。
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def _backends():
    from panbridge.providers.aliyun.qr_backend import AliyunQrBackend
    from panbridge.providers.pan115.qr_backend import Pan115QrBackend
    from panbridge.providers.pan123.qr_backend import Pan123QrBackend
    from panbridge.providers.quark.qr_backend import QuarkQrBackend
    from panbridge.providers.uc.qr_backend import UcQrBackend

    return [
        ("quark", QuarkQrBackend),
        ("aliyun", AliyunQrBackend),
        ("115", Pan115QrBackend),
        ("123", Pan123QrBackend),
        ("uc", UcQrBackend),
    ]


async def _check_one(name: str, cls) -> bool:
    try:
        backend = cls()
    except RuntimeError as exc:
        print(f"[{name:8s}] SKIP 依赖缺失: {exc}")
        return True  # 依赖缺失不算失败

    try:
        ticket = await asyncio.wait_for(backend.start(), timeout=35)
    except TimeoutError:
        print(f"[{name:8s}] FAIL 超时(35s)")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[{name:8s}] FAIL {exc.__class__.__name__}: {exc}")
        return False

    qr = ticket.qr_url or ""
    if not qr:
        print(f"[{name:8s}] FAIL 返回空二维码")
        return False

    print(f"[{name:8s}] OK   qr_url={qr[:90]!r}")
    return True


async def _main(provider: str | None) -> int:
    all_ok = True
    for name, cls in _backends():
        if provider and name != provider:
            continue
        if not await _check_one(name, cls):
            all_ok = False
    return 0 if all_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="扫码登录协议连通性检查")
    parser.add_argument("--provider", help="只检查单个平台（如 quark）")
    args = parser.parse_args()
    return asyncio.run(_main(args.provider))


if __name__ == "__main__":
    sys.exit(main())
