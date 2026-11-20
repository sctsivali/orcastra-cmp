"""Alembic environment: online and offline migrations against async PostgreSQL."""

import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Put backend/ on sys.path so `app` imports from any working directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import Base
from app.models import db_models
# important: Base.metadata only sees model modules imported here. A missing one produces
# no autogenerate diff and no schema-parity failure, so its tables never get created.
# Import every module that holds tables.
from app.models import api_key_usage_models  # noqa: F401
from app.models import cluster_registration_models  # noqa: F401
from app.models import extension_models  # noqa: F401
from app.models import migration_models  # noqa: F401
from app.models import project_delete_models  # noqa: F401
from app.models import snapshot_schedule_models  # noqa: F401
from app.models import task_journal_models  # noqa: F401
from app.models import trust_cleanup_models  # noqa: F401
from app.models import tenant_rule_models  # noqa: F401
from app.models import my_key_models  # noqa: F401
from app.models import branding_models  # noqa: F401
from app.models import mini_identity_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate compares the database against this metadata.
target_metadata = Base.metadata

# Required. There is no hardcoded fallback, so no credential lives in the source.
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError(
        "DATABASE_URL environment variable is required. "
        "Format: postgresql+asyncpg://user:password@host:port/database"
    )

# The async engine needs the asyncpg driver named in the URL scheme.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

config.set_main_option("sqlalchemy.url", DATABASE_URL)


def run_migrations_offline() -> None:
    """Offline mode: emit SQL from the URL alone, with no Engine and no DBAPI."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Online mode: run the migrations on a connection from an async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
