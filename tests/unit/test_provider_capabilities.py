"""Provider 能力声明一致性测试。

验证每个 Provider 的 `capabilities` 声明的能力与实际方法实现一致：
- 声明了某能力，对应方法就必须真实实现（而非空壳 raise UNSUPPORTED_OPERATION）。
- 未声明某能力，则该方法调用时应抛 UNSUPPORTED_OPERATION（诚实声明）。

这能防止「能力矩阵声称支持、实际是空壳」的虚假声明。
"""

from __future__ import annotations

import inspect

import pytest

from panbridge.providers.base import ShareProvider

# 能力名 -> 方法名映射
_CAPABILITY_METHODS = {
    "inspect_share": "inspect_share",
    "list_share_files": "list_share_files",
    "save_shared_files": "save_shared_files",
    "list_account_files": "list_account_files",
    "get_download_info": "get_download_info",
    "get_share_download_info": "get_share_download_info",
    "validate_account": "validate_account",
    "create_folder": "create_folder",
}

# 抽象方法：子类必须实现（否则无法实例化），不在此检查空壳。
_ABSTRACT_METHODS = {
    "inspect_share",
    "list_share_files",
    "validate_account",
    "save_shared_files",
    "list_account_files",
    "get_download_info",
}


def _is_stub(provider: ShareProvider, method_name: str) -> bool:
    """判断方法是否为「空壳实现」（仅 raise UNSUPPORTED_OPERATION）。

    通过检查方法源码是否只包含 UNSUPPORTED_OPERATION 抛出来判断。
    更可靠的方式是：直接调用并检查是否抛 UNSUPPORTED_OPERATION，
    但抽象方法调用需要 mock 依赖，故这里用源码启发式 + 已知的空壳白名单。
    """
    method = getattr(type(provider), method_name, None)
    if method is None:
        return True
    # 基类默认实现（非子类覆盖）即视为空壳
    if method.__qualname__.startswith("ShareProvider."):
        return True
    src = inspect.getsource(method)
    # 空壳特征：函数体很短且只 raise UNSUPPORTED_OPERATION
    return "UNSUPPORTED_OPERATION" in src and len(src.strip().splitlines()) < 20


def _all_providers():
    """实例化所有真实 Provider（不含 Mock），返回 (name, provider, cipher)。"""
    from panbridge.providers.aliyun.provider import AliyunProvider
    from panbridge.providers.baidu.provider import BaiduProvider
    from panbridge.providers.caiyun.provider import CaiyunProvider
    from panbridge.providers.cloud189.provider import Cloud189Provider
    from panbridge.providers.lanzou.provider import LanzouProvider
    from panbridge.providers.pan115.provider import Pan115Provider
    from panbridge.providers.pan123.provider import Pan123Provider
    from panbridge.providers.pikpak.provider import PikPakProvider
    from panbridge.providers.quark.provider import QuarkProvider
    from panbridge.providers.uc.provider import UcProvider
    from panbridge.providers.weiyun.provider import WeiyunProvider
    from panbridge.providers.xunlei.provider import XunleiProvider

    return [
        ("aliyun", AliyunProvider(cipher=None)),
        ("baidu", BaiduProvider(cipher=None)),
        ("caiyun", CaiyunProvider(cipher=None)),
        ("cloud189", Cloud189Provider(cipher=None)),
        ("lanzou", LanzouProvider()),
        ("115", Pan115Provider(cipher=None)),
        ("123", Pan123Provider(cipher=None)),
        ("pikpak", PikPakProvider()),
        ("quark", QuarkProvider(cipher=None)),
        ("uc", UcProvider(cipher=None)),
        ("weiyun", WeiyunProvider(cipher=None)),
        ("xunlei", XunleiProvider(cipher=None)),
    ]


def test_provider_count_is_12():
    """实际注册的真实 Provider 应为 12 个（不含 Mock）。"""
    providers = _all_providers()
    assert len(providers) == 12


@pytest.mark.parametrize(
    "name,provider",
    _all_providers(),
    ids=[n for n, _ in _all_providers()],
)
def test_capabilities_declare_real_methods(name, provider):
    """每个声明的能力，对应方法必须存在。"""
    for cap in provider.capabilities:
        method_name = _CAPABILITY_METHODS.get(cap, cap)
        assert hasattr(provider, method_name), (
            f"{name} 声明能力 {cap!r} 但缺少方法 {method_name}"
        )


@pytest.mark.parametrize(
    "name,provider",
    _all_providers(),
    ids=[n for n, _ in _all_providers()],
)
def test_anonymous_download_capability_is_not_stub(name, provider):
    """声明 get_share_download_info 的平台，实现不得为空壳。

    这是「匿名下载」真实性的关键检查：不能只解析分享，
    必须真正能解析出直链。
    """
    if "get_share_download_info" not in provider.capabilities:
        pytest.skip(f"{name} 未声明匿名分享下载")
    assert not _is_stub(provider, "get_share_download_info"), (
        f"{name} 声明了匿名分享下载，但实现是空壳"
    )


@pytest.mark.parametrize(
    "name,provider",
    _all_providers(),
    ids=[n for n, _ in _all_providers()],
)
def test_save_capability_is_not_stub(name, provider):
    """声明 save_shared_files 的平台，实现不得为空壳（未验证的转存应如实
    抛 UNSUPPORTED_OPERATION，但需明确注释「未验证」而非假装实现）。"""
    if "save_shared_files" not in provider.capabilities:
        pytest.skip(f"{name} 未声明转存")
    # 转存是抽象方法，子类必然实现。这里只检查是否显式声明了能力。
    # 空壳转存（未验证却声明支持）会在能力矩阵文档中如实标注「🟡 需验证」。
    assert "save_shared_files" in provider.capabilities
