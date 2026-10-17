"""Worker 独立入口。

单独进程运行后台任务执行循环，与 API 服务解耦。
"""

from __future__ import annotations

import asyncio

from panbridge.infrastructure.config import Settings
from panbridge.main import build_app


async def main() -> None:
    settings = Settings.from_env()
    app, worker = build_app(settings)

    from panbridge.persistence import create_engine, init_db

    engine = create_engine(settings.database_url)
    await init_db(engine)

    try:
        await worker.run()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
