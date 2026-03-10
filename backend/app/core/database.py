"""
Database Configuration for Orcastra Dashboard.
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
    Initialize database tables.
    Called on application startup.
    """
    try:
        async with engine.begin() as conn:
            # Import all models here to ensure they're registered
            from app.models import db_models
            
            # Create all tables
            await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise


async def close_db() -> None:
    """
    Close database connection.
    Called on application shutdown.
    """
    await engine.dispose()
    logger.info("Database connection closed")
