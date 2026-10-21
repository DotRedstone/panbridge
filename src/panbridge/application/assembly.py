"""应用装配：Provider 注册、认证服务、下载器的统一构建。

此前 `main.py`、`cli.py`、`scripts/acceptance.py` 各自复制了一份
Provider 注册清单，容易漏注册（例如 MockProvider 只在 main 里注册）
或配置不一致（下载器参数不同）。本模块收敛这些装配逻辑，供所有入口
复用，避免重复代码漂移。

职责边界（遵守 AGENTS.md「入口只组装」）：
- 本模块只做「把零件装起来」，不做业务逻辑。
- 新增平台：在 `_REGISTER_ALL` 内加一行即可，所有入口自动生效。
"""

from __future__ import annotations

import logging

from panbridge.application.auth import QrAuthService
from panbridge.application.auth_service import AuthService
from panbridge.application.service import ApplicationService
from panbridge.downloads.http import HttpDownloader
from panbridge.infrastructure.config import Settings
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


def build_registry(
    cipher: CredentialCipher | None = None,
    *,
    include_mock: bool = False,
) -> ProviderRegistry:
    """构建 Provider 注册表。

    - `cipher`：用于需要解密账号凭据的 Provider；为 None 时这些 Provider
      仍注册，但运行时对需要登录的操作会如实报错（ACCOUNT_EXPIRED /
      INTERNAL_ERROR），而非伪造成功。
    - `include_mock`：是否注册 MockProvider（测试/演示用）。生产入口默认
      关闭，测试与验收脚本按需开启。
    """
    from panbridge.providers.aliyun.provider import AliyunProvider
    from panbridge.providers.baidu.provider import BaiduProvider
    from panbridge.providers.caiyun.provider import CaiyunProvider
    from panbridge.providers.cloud189.provider import Cloud189Provider
    from panbridge.providers.guangya.provider import GuangyaProvider
    from panbridge.providers.lanzou.provider import LanzouProvider
    from panbridge.providers.pan115.provider import Pan115Provider
    from panbridge.providers.pan123.provider import Pan123Provider
    from panbridge.providers.pikpak.provider import PikPakProvider
    from panbridge.providers.quark.provider import QuarkProvider
    from panbridge.providers.uc.provider import UcProvider
    from panbridge.providers.weiyun.provider import WeiyunProvider
    from panbridge.providers.wukong.provider import WukongProvider
    from panbridge.providers.xunlei.provider import XunleiProvider

    registry = ProviderRegistry()
    if include_mock:
        from panbridge.providers.quark.mock import MockProvider

        registry.register(MockProvider())
    # 夸克：扫码登录 + 转存/下载（需 Cookie，可加密）。
    registry.register(QuarkProvider(cipher=cipher))
    # 阿里云盘：分享解析/直链下载无需登录，转存需 refresh_token。
    registry.register(AliyunProvider(cipher=cipher))
    # 蓝奏云：匿名解析 + 直链下载，无需账号。
    registry.register(LanzouProvider())
    # 123 云盘：匿名分享解析 + 直链下载，转存需账号。
    registry.register(Pan123Provider(cipher=cipher))
    # 百度网盘：匿名解析分享列表，直链下载需 Cookie。
    registry.register(BaiduProvider(cipher=cipher))
    # 115 网盘：匿名解析分享，转存/下载需 Cookie。
    registry.register(Pan115Provider(cipher=cipher))
    # PikPak：匿名解析分享 + 直链下载，转存需账号。
    registry.register(PikPakProvider())
    # 天翼云盘：账号文件列表 + 下载，分享解析需登录。
    registry.register(Cloud189Provider(cipher=cipher))
    # 中国移动云盘：分享解析/转存/账号列表，需 Authorization。
    registry.register(CaiyunProvider(cipher=cipher))
    # UC 网盘：分享解析/转存/账号列表，需 Cookie。
    registry.register(UcProvider(cipher=cipher))
    # 迅雷云盘：分享解析/转存/账号列表，需 refresh_token。
    registry.register(XunleiProvider(cipher=cipher))
    # 腾讯微云：匿名解析分享，转存/下载需 skey Cookie。
    registry.register(WeiyunProvider(cipher=cipher))
    # 光鸭云盘：匿名解析分享，转存/账号列表/下载需 access_token。
    registry.register(GuangyaProvider(cipher=cipher))
    # 悟空网盘：匿名解析分享，转存/账号列表/下载需 Cookie 或 token。
    registry.register(WukongProvider(cipher=cipher))
    return registry


def build_downloader(settings: Settings) -> HttpDownloader:
    """按配置构建下载器。

    下载量上限 `max_size` 取自 `PANBRIDGE_MAX_DOWNLOAD_BYTES`（默认 0
    表示不限），与验收脚本的 `--max-download-bytes` 语义对齐，用于在
    下载器层面硬性拦截单文件超限，避免意外下载超大文件。
    """
    return HttpDownloader(
        rate_limit=settings.download_rate_limit or None,
        max_size=settings.max_download_bytes or None,
        allow_loopback=settings.allow_insecure_download,
    )


def build_auth_service(
    service: ApplicationService,
    registry: ProviderRegistry,
    sessionmaker,
    cipher: CredentialCipher | None,
) -> AuthService | None:
    """构建认证服务；无 cipher（未配置主密钥）时返回 None。

    扫码登录支持夸克、阿里云盘、115 网盘、123 云盘四个平台；其余平台需
    手工导入凭据（见 docs/providers.md）。
    """
    if cipher is None:
        return None
    qr_auth = QrAuthService(sessionmaker, cipher)

    # 注册各平台扫码后端；后端初始化失败（依赖缺失）时不阻断其他平台。
    from panbridge.providers.aliyun.qr_backend import AliyunQrBackend
    from panbridge.providers.pan115.qr_backend import Pan115QrBackend
    from panbridge.providers.pan123.qr_backend import Pan123QrBackend
    from panbridge.providers.quark.qr_backend import QuarkQrBackend
    from panbridge.providers.uc.qr_backend import UcQrBackend

    for backend_cls in (
        QuarkQrBackend,
        AliyunQrBackend,
        Pan115QrBackend,
        Pan123QrBackend,
        UcQrBackend,
    ):
        try:
            qr_auth.register_backend(backend_cls())
        except RuntimeError as exc:  # 依赖缺失，跳过该平台
            logger.warning("qr backend %s unavailable: %s",
                           backend_cls.__name__, exc)
    return AuthService(service, registry, qr_auth)
