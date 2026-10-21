"""panbridge 命令行入口。

提供扫码登录、凭据导入、登录状态查询、退出登录，无需 WebUI。

用法：
    panbridge auth login --provider quark    # 终端显示二维码，夸克 App 扫码
    panbridge auth login --provider aliyun   # 阿里云盘 App 扫码
    panbridge auth login --provider 115      # 115 App 扫码
    panbridge auth login --provider 123      # 123 云盘 App 扫码
    panbridge auth login --provider uc       # UC 网盘 App 扫码
    # 手工导入凭据（安全：不经过命令行参数，交互式无回显输入）
    panbridge auth import --provider baidu            # 提示后无回显输入
    panbridge auth import --provider baidu --label work
    panbridge auth status                    # 查询当前登录状态
    panbridge auth logout                    # 退出登录（删除本地账号）
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
import tempfile

from panbridge.application.assembly import (
    build_auth_service,
    build_registry,
)
from panbridge.application.service import ApplicationService
from panbridge.infrastructure.config import Settings
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.persistence import (
    create_engine,
    create_sessionmaker,
    init_db,
)

logging.basicConfig(level=logging.WARNING)


def _display_qr(qr_url: str, provider: str) -> None:
    """渲染登录二维码：终端 ASCII + 落盘 PNG 图片文件（方便双击打开扫描）。

    - ASCII：供纯终端环境直接扫。
    - PNG：写到系统临时目录（不污染项目根目录），打印绝对路径，
      用户用图片查看器打开后拿手机扫码即可。
    - 兜底：渲染失败时打印登录链接（不含凭据，可浏览器打开）。
    """
    print(f"\n请使用「{provider}」App 扫描下方二维码登录：\n")
    png_path: str | None = None
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(qr_url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        # 额外落盘一张 PNG，便于双击打开后用手机扫描。
        img = qr.make_image(fill_color="black", back_color="white")
        png_path = os.path.join(
            tempfile.gettempdir(), f"panbridge-{provider}-login.png"
        )
        img.save(png_path)
    except Exception:  # noqa: BLE001
        # 二维码渲染失败时，给出可手动打开的链接（不包含凭据）
        print(f"二维码渲染失败，请在浏览器中打开：{qr_url}")
    if png_path:
        print(f"\n💡 已生成二维码图片：{png_path}")
        print("   （可用图片查看器打开，用手机 App 扫码，比终端黑块更清晰）")
    print("\n（也可在浏览器打开登录链接）\n")


def _build(settings: Settings):
    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    cipher = CredentialCipher(settings.secret_key)
    registry = build_registry(cipher)
    service = ApplicationService(registry, sessionmaker)
    auth = build_auth_service(service, registry, sessionmaker, cipher)
    return engine, auth, service, cipher


async def _login(provider: str) -> int:
    settings = Settings.from_env()
    engine, auth, _service, _cipher = _build(settings)
    await init_db(engine)
    if auth is None:
        print("❌ 未配置 PANBRIDGE_SECRET_KEY，无法加密存储账号凭据。")
        return 1
    try:
        info = await auth.start_qr_login(provider)
        session_id = info["session_id"]
        _display_qr(info["qr_url"], info.get("display_name") or info["provider"])

        print("等待扫码登录中（最多 5 分钟）...\n")
        for _ in range(150):  # 每 2 秒轮询一次
            await asyncio.sleep(2)
            status = await auth.check_qr_login(session_id)
            if status.get("status") == "success":
                result = await auth.finish_qr_login(session_id)
                print(
                    "✅ 登录成功！账号已保存。\n"
                    f"   account_id = {result['account_id']}\n"
                    f"   provider   = {result['provider']}"
                )
                return 0
            if status.get("status") == "expired":
                print("⏰ 二维码已过期，请重新运行 `panbridge auth login`")
                return 1
            if status.get("status") == "cancelled":
                print("🚫 扫码已取消")
                return 1

        print("⏰ 登录超时，请重新运行 `panbridge auth login`")
        return 1
    finally:
        await engine.dispose()


async def _status(provider: str) -> int:
    settings = Settings.from_env()
    engine, auth, _service, _cipher = _build(settings)
    await init_db(engine)
    if auth is None:
        print("❌ 未配置 PANBRIDGE_SECRET_KEY，无法查询登录状态。")
        return 1
    try:
        status = await auth.login_status(provider)
        if status.get("logged_in"):
            account = status.get("account") or {}
            print("✅ 已登录")
            print(f"   account_id = {account.get('id')}")
            print(f"   label      = {account.get('label')}")
            print(f"   provider   = {account.get('provider')}")
        elif status.get("expired"):
            print("⚠️  已保存账号，但凭据已失效，请重新扫码登录：")
            print("   panbridge auth login")
        else:
            print("ℹ️  未登录")
            print("   请运行 `panbridge auth login` 扫码登录")
        return 0
    finally:
        await engine.dispose()


async def _logout(provider: str) -> int:
    settings = Settings.from_env()
    engine, auth, _service, _cipher = _build(settings)
    await init_db(engine)
    if auth is None:
        print("❌ 未配置 PANBRIDGE_SECRET_KEY，无法退出登录。")
        return 1
    try:
        result = await auth.logout(provider)
        print(f"✅ 已退出登录（移除 {result['removed']} 个账号）")
        return 0
    finally:
        await engine.dispose()


async def _import(provider: str, label: str, credential: str | None) -> int:
    """安全导入手工凭据（Cookie/token/refresh_token），加密入库。

    适用于尚未实现扫码登录的平台（百度/UC/迅雷/微云等），凭据由用户
    自行从浏览器/客户端获取后提供。凭据立即加密存储，不落明文。

    安全约束（凭据绝不进入命令行参数/进程列表/Shell 历史）：
    - 默认从 TTY 无回显交互输入（getpass），输入内容不出现在终端。
    - 非交互环境（无 TTY）下从环境变量 `PANBRIDGE_IMPORT_CREDENTIAL`
      读取（用于 CI/脚本），或从 stdin 管道读取。
    - `--credential` 仅为兼容旧脚本保留，但会打印明确警告，强烈建议
      改用无参数交互输入。
    """
    settings = Settings.from_env()
    engine, auth, service, cipher = _build(settings)
    await init_db(engine)
    if auth is None:
        print("❌ 未配置 PANBRIDGE_SECRET_KEY，无法加密存储账号凭据。")
        return 1
    try:
        if credential is None:
            credential = _read_credential_securely()
        else:
            print(
                "⚠️  警告：通过 --credential 参数传入凭据会将其写入 Shell "
                "历史与进程列表，存在泄漏风险。建议改为不带 --credential "
                "运行，使用交互式无回显输入。"
            )
        if not credential or not credential.strip():
            print("❌ 凭据为空，已取消导入。")
            return 1
        encrypted = cipher.encrypt(credential.strip())
        account = await service.add_account(
            provider, label or f"{provider}-manual", encrypted
        )
        print(
            "✅ 凭据已安全导入并加密保存。\n"
            f"   account_id = {account.id}\n"
            f"   provider   = {account.provider}\n"
            f"   label      = {account.label}"
        )
        return 0
    finally:
        await engine.dispose()


def _read_credential_securely() -> str:
    """安全读取凭据：优先交互无回显，其次环境变量，最后 stdin 管道。

    返回读到的凭据字符串（可能为空）。绝不把凭据打印到终端或日志。
    """
    # 1) 交互式 TTY：getpass 无回显输入
    if sys.stdin.isatty():
        return getpass.getpass("请输入凭据（Cookie/token，输入不回显）: ")

    # 2) 环境变量（CI/脚本，非交互）
    env_val = os.environ.get("PANBRIDGE_IMPORT_CREDENTIAL")
    if env_val:
        return env_val

    # 3) stdin 管道（echo "..." | panbridge auth import ...）
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data:
            return data.strip()

    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="panbridge",
        description="PanBridge 多网盘转存下载后端 CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    auth_parser = sub.add_parser("auth", help="账号认证管理")
    auth_sub = auth_parser.add_subparsers(dest="auth_command", required=True)

    login_p = auth_sub.add_parser(
        "login", help="扫码登录（quark/aliyun/115/123/uc）"
    )
    login_p.add_argument("--provider", default="quark", help="网盘平台")

    import_p = auth_sub.add_parser("import", help="手工导入凭据（加密入库）")
    import_p.add_argument("--provider", required=True, help="网盘平台")
    import_p.add_argument("--label", default="", help="账号标签（可选）")
    import_p.add_argument(
        "--credential",
        default=None,
        help=(
            "凭据（Cookie/token）。【不安全】会写入 Shell 历史，"
            "建议省略此参数，改用交互式无回显输入。"
        ),
    )

    status_p = auth_sub.add_parser("status", help="查询登录状态")
    status_p.add_argument("--provider", default="quark", help="网盘平台")

    logout_p = auth_sub.add_parser("logout", help="退出登录")
    logout_p.add_argument("--provider", default="quark", help="网盘平台")

    args = parser.parse_args(argv)

    if args.auth_command == "login":
        return asyncio.run(_login(args.provider))
    if args.auth_command == "import":
        return asyncio.run(_import(args.provider, args.label, args.credential))
    if args.auth_command == "status":
        return asyncio.run(_status(args.provider))
    if args.auth_command == "logout":
        return asyncio.run(_logout(args.provider))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
