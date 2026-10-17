"""Provider 注册表。

通过统一入口将平台名映射到适配器实例。
新增平台只需实现 ShareProvider 并在此注册。
"""

from __future__ import annotations

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.providers.base import ShareProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ShareProvider] = {}

    def register(self, provider: ShareProvider) -> None:
        if provider.name in self._providers:
            raise ValueError(f"provider already registered: {provider.name}")
        self._providers[provider.name] = provider

    def get(self, name: str) -> ShareProvider:
        try:
            return self._providers[name]
        except KeyError:
            raise DomainError(
                ErrorCode.UNSUPPORTED_PROVIDER,
                f"unsupported provider: {name}",
                details={"provider": name},
            ) from None

    def names(self) -> list[str]:
        return list(self._providers.keys())

    def list_meta(self) -> list[dict]:
        return [
            {
                "name": p.name,
                "display_name": p.display_name,
                "capabilities": sorted(p.capabilities),
            }
            for p in self._providers.values()
        ]
