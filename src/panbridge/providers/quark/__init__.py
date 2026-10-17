"""夸克网盘 Provider。

包含：
- MockProvider：无账号环境验证（测试/演示用）
- QuarkProvider：真实接入（基于 QuarkPan，MIT）
"""

from panbridge.providers.quark.mock import MockProvider
from panbridge.providers.quark.provider import QuarkProvider

__all__ = ["MockProvider", "QuarkProvider"]
