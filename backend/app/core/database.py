"""
Database Configuration for Orcastra CMP.
Uses SQLAlchemy async with PostgreSQL.
"""

import os
import logging
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import AsyncAdaptedQueuePool

logger = logging.getLogger(__name__)

# Database URL from environment (REQUIRED - no hardcoded fallback for security)
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError(
        "DATABASE_URL environment variable is required. "
        "Format: postgresql+asyncpg://user:password@host:port/database"
    )

# Connection pool config (tunable via env)
_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "20"))
_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "30"))
_POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "30"))
_POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800"))  # 30 min

# Create async engine with proper connection pooling
engine = create_async_engine(
    DATABASE_URL,
    echo=(os.getenv("DEBUG", "false").lower() == "true"
          and os.getenv("ENVIRONMENT", "development") == "development"),
    poolclass=AsyncAdaptedQueuePool,
    pool_size=_POOL_SIZE,
    max_overflow=_MAX_OVERFLOW,
    pool_timeout=_POOL_TIMEOUT,
    pool_recycle=_POOL_RECYCLE,
    pool_pre_ping=True,  # verify connection health before checkout
)

# Create async session factory
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Base class for all database models."""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency to get database session.
    """
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    """
    Verify the database is reachable and under Alembic control.

    This deliberately does NOT create tables. Releases up to v1.0.0-RC2 built their
    schema here with `Base.metadata.create_all`, which silently diverged from the
    migrations (the `user_preferences` table, the `uq_org_source_external_id`
    uniqueness backstop and two NOT NULL columns all went missing on real
    deployments) and left databases with no Alembic stamp, so `alembic upgrade head`
    could not run at all. Alembic is now the single source of truth; the entrypoint
    runs migrations once per container before this ever executes.

    note: this reports rather than raises. `lifespan()` already downgrades failures
    here to a warning, and refusing to boot is the entrypoint's job, not ours.
    """
    from sqlalchemy import text

    async with engine.connect() as conn:
        stamped = (
            await conn.execute(text("SELECT to_regclass('public.alembic_version')"))
        ).scalar()
        if stamped is None:
            logger.error(
                "Database has no alembic_version table. Schema is not under migration "
                "control and the application will misbehave. Run `alembic upgrade head` "
                "(the container entrypoint does this unless RUN_MIGRATIONS=false)."
            )
            return

        current = (
            await conn.execute(text("SELECT version_num FROM alembic_version"))
        ).scalars().all()

    if not current:
        logger.error(
            "alembic_version exists but is empty. Run `alembic upgrade head` before serving."
        )
        return

    expected = _alembic_heads()
    if expected and set(current) != set(expected):
        logger.error(
            "Database schema is at %s but this build expects %s. Run `alembic upgrade head`.",
            ", ".join(sorted(current)),
            ", ".join(sorted(expected)),
        )
        return

    logger.info("Database schema verified at revision %s", ", ".join(sorted(current)))


def _alembic_heads() -> list[str]:
    """Head revision(s) shipped in this build, or [] if the script directory is unreadable."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cfg = Config(os.path.join(backend_dir, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))
        return list(ScriptDirectory.from_config(cfg).get_heads())
    except Exception as exc:  # pragma: no cover - diagnostics only, never fatal
        logger.debug("Could not resolve alembic heads: %s", exc)
        return []


async def close_db() -> None:
    """
    Close database connection.
    Called on application shutdown.
    """
    await engine.dispose()
    logger.info("Database connection closed")
