"""真实匿名下载验证脚本（不依赖账号）。

对支持匿名解析的平台，用公开分享实际验证：
1. inspect_share 解析分享（得到文件列表）
2. get_share_download_info 解析单个小文件的直链
3. HttpDownloader 实际下载该直链，校验文件大小与内容非空

仅选取「小文件」验证，避免下载大型资源。无效链接返回正确错误码
不视为成功；必须真实拿到直链并落盘才算验证通过。

用法：
    PYTHONPATH=src python scripts/e2e_anon_download.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from panbridge.downloads.http import HttpDownloader
from panbridge.providers.lanzou.provider import LanzouProvider
from panbridge.providers.pan123.provider import Pan123Provider

# 已验证有效的公开分享（小文件，来源 423down）
# 123 云盘：目录分享 A6cA-I24Jh
PAN123_SHARE = "A6cA-I24Jh"
PAN123_PASSWORD = None


async def _verify_provider(name: str, provider, share_id: str, password, tmpdir):
    print(f"\n=== 验证平台：{name} ===")
    # 1. 解析分享
    try:
        share = await provider.inspect_share(share_id, password)
    except Exception as exc:  # noqa: BLE001
        print(f"  [解析] 失败: {type(exc).__name__}: {exc}")
        return False

    files = [f for f in share.files if not f.is_dir]
    print(f"  [解析] 标题={share.title} 文件={len(share.files)} 个"
          f"（非目录 {len(files)}）")

    if not files:
        print("  [解析] 无可用非目录文件（可能全是目录），跳过下载验证")
        return False

    # 选最小的文件验证下载
    smallest = min(files, key=lambda f: f.size or 0)
    print(f"  [下载] 目标文件: {smallest.name} (id={smallest.file_id}, "
          f"size={smallest.size})")

    # 2. 解析直链
    try:
        info = await provider.get_share_download_info(
            share_id, password, smallest.file_id
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [直链] 失败: {type(exc).__name__}: {exc}")
        return False

    url = info.get("url")
    name = info.get("name") or smallest.name
    if not url:
        print("  [直链] 返回空 URL，失败")
        return False
    print(f"  [直链] url={url[:120]}... name={name}")

    # 3. 实际下载（限制最大 5MB，避免下载大文件）
    downloader = HttpDownloader(timeout=30.0, max_size=5 * 1024 * 1024)
    dest = os.path.join(tmpdir, name or f"{name}.bin")
    try:
        async for prog in downloader.download(
            url, dest, headers=info.get("headers") or {}
        ):
            pass
        size = os.path.getsize(dest)
        print(f"  [下载] 完成 size={size} bytes -> {dest}")
        if size == 0:
            print("  [下载] 文件为空，失败")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  [下载] 失败: {type(exc).__name__}: {exc}")
        return False


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="panbridge-anon-")
    print(f"临时下载目录: {tmpdir}")

    results: dict[str, bool] = {}

    # 123 云盘（匿名解析 + 匿名直链下载）
    try:
        results["123"] = await _verify_provider(
            "123 云盘", Pan123Provider(cipher=None),
            PAN123_SHARE, PAN123_PASSWORD, tmpdir,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"123 云盘异常: {type(exc).__name__}: {exc}")
        results["123"] = False

    # 蓝奏云（需一个有效的公开分享链接，脚本支持环境变量注入）
    lanzou_share = os.environ.get("LANZOU_SHARE", "")
    if lanzou_share:
        try:
            results["lanzou"] = await _verify_provider(
                "蓝奏云", LanzouProvider(), lanzou_share, None, tmpdir,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"蓝奏云异常: {type(exc).__name__}: {exc}")
            results["lanzou"] = False
    else:
        print("\n[跳过] 未提供蓝奏云分享链接（export LANZOU_SHARE=...）")

    print("\n=== 验证结果汇总 ===")
    for name, ok in results.items():
        print(f"  {name}: {'✅ 真实匿名下载通过' if ok else '❌ 未通过'}")


if __name__ == "__main__":
    asyncio.run(main())
