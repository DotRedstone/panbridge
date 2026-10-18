"""应用配置。

通过环境变量注入，主密钥等敏感项由外部 secret 提供。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    api_key: str = ""
    database_url: str = "sqlite+aiosqlite:///./panbridge.db"
    # 主密钥用于凭据加密，必须通过外部注入
    secret_key: str = ""
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    download_dir: str = "./downloads"
    max_task_concurrency: int = 4
    worker_lease_seconds: int = 300
    max_retry_attempts: int = 3
    allow_insecure_download: bool = False
    # 下载限速（字节/秒），0 或负数表示不限速
    download_rate_limit: int = 0
    # 单文件下载量上限（字节），0 表示不限。由下载器硬性拦截，防止
    # 意外下载超大文件（配合验收脚本的 --max-download-bytes 使用）。
    max_download_bytes: int = 0

    @classmethod
    def from_env(cls) -> Settings:
        # 主密钥优先环境变量；未配置时自动生成并持久化到用户目录，
        # 本地单用户无需手工填写（见 infrastructure/secret_key.py）。
        from panbridge.infrastructure.secret_key import load_or_create_secret_key

        return cls(
            api_key=os.environ.get("PANBRIDGE_API_KEY", ""),
            database_url=os.environ.get(
                "PANBRIDGE_DATABASE_URL",
                "sqlite+aiosqlite:///./panbridge.db",
            ),
            secret_key=load_or_create_secret_key(),
            bind_host=os.environ.get("PANBRIDGE_BIND_HOST", "127.0.0.1"),
            bind_port=int(os.environ.get("PANBRIDGE_BIND_PORT", "8000")),
            download_dir=os.environ.get(
                "PANBRIDGE_DOWNLOAD_DIR", "./downloads"
            ),
            max_task_concurrency=int(
                os.environ.get("PANBRIDGE_MAX_TASK_CONCURRENCY", "4")
            ),
            worker_lease_seconds=int(
                os.environ.get("PANBRIDGE_WORKER_LEASE_SECONDS", "300")
            ),
            max_retry_attempts=int(
                os.environ.get("PANBRIDGE_MAX_RETRY_ATTEMPTS", "3")
            ),
            download_rate_limit=int(
                os.environ.get("PANBRIDGE_DOWNLOAD_RATE_LIMIT", "0")
            ),
            max_download_bytes=int(
                os.environ.get("PANBRIDGE_MAX_DOWNLOAD_BYTES", "0")
            ),
            allow_insecure_download=(
                os.environ.get("PANBRIDGE_ALLOW_INSECURE_DOWNLOAD", "") == "1"
            ),
        )


# 默认配置，可在测试中覆盖
settings = Settings.from_env()
