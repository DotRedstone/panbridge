"""Alembic 迁移环境。

MVP 阶段使用 SQLAlchemy `create_all` 自动建表，
alembic 在此作为后续正式迁移的骨架保留。
"""

from __future__ import annotations

from panbridge.persistence.models import Base

target_metadata = Base.metadata
