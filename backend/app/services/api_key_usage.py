"""When each external API key was last used (app/models/api_key_usage_models.py).

Readers take the newer of this and the `last_used_at` stored on the key document, so a key with
no recorded use here keeps showing that older time.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional, Set

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.models.api_key_usage_models import ApiKeyUsage

logger = logging.getLogger(__name__)

# Tests point this at SQLite. None means the application's own session maker.
session_maker: Optional[Callable[[], Any]] = None
_background: Set[asyncio.Task] = set()


def _maker() -> Callable[[], Any]:
    if session_maker is not None:
        return session_maker
    from app.core.database import async_session_maker

    return async_session_maker


def _aware(value: datetime) -> datetime:
    # SQLite hands timestamps back without a zone; every one stored here is UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def record_use(key_id: str) -> None:
    """Move the key's last use forward to now; never backwards, never raises."""
    now = datetime.now(timezone.utc)
    try:
        async with _maker()() as db:
            for _attempt in range(2):
                row = await db.get(ApiKeyUsage, key_id)
                if row is None:
                    db.add(ApiKeyUsage(key_id=key_id, last_used_at=now))
                elif _aware(row.last_used_at) < now:
                    row.last_used_at = now
                try:
                    await db.commit()
                    return
                except IntegrityError:
                    # Another worker recorded this key's first use between the read and the insert.
                    await db.rollback()
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never fail or slow a request
        logger.debug("last use of %s not recorded: %s", key_id, exc)


def schedule_use(key_id: str) -> None:
    """Record a use without awaiting it, holding a reference until it is written."""
    try:
        task = asyncio.get_running_loop().create_task(record_use(key_id))
    except RuntimeError:  # no loop: nothing to record on
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


async def last_used(key_ids: Iterable[str]) -> Dict[str, str]:
    """{key id: ISO time with offset} for the keys that have a recorded use; empty if unreadable."""
    ids = sorted({k for k in key_ids if k})
    if not ids:
        return {}
    try:
        async with _maker()() as db:
            rows = (await db.execute(select(ApiKeyUsage).where(ApiKeyUsage.key_id.in_(ids)))).scalars().all()
    except Exception as exc:  # noqa: BLE001 - a listing must not fail for a bookkeeping field
        logger.warning("Could not read API key usage: %s", exc)
        return {}
    return {row.key_id: _aware(row.last_used_at).isoformat() for row in rows}


def newer(stored: Optional[str], recorded: Optional[str]) -> Optional[str]:
    """The later of the key document's legacy value and the recorded use."""
    if not stored:
        return recorded
    if not recorded:
        return stored
    try:
        return max(stored, recorded, key=lambda value: _aware(datetime.fromisoformat(value)))
    except ValueError:
        return recorded


async def forget(key_id: str) -> bool:
    """Remove a deleted key's row; False when it could not be removed."""
    try:
        async with _maker()() as db:
            await db.execute(delete(ApiKeyUsage).where(ApiKeyUsage.key_id == key_id))
            await db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - a deleted key's row reports nothing to anyone
        logger.warning("Could not remove the usage row of %s: %s", key_id, exc)
        return False
