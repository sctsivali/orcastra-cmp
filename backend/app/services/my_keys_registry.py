"""Keep the My Keys registry (models/my_key_models.py) in step with Vault, for the audit trail only.

Every write here is best effort: the key itself lives in Vault and the request that touched it
has already succeeded or failed on its own terms. A registry write that fails is logged and the
caller carries on; the next roster read backfills what is missing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import select

from app.models.my_key_models import MyKeyRecord

logger = logging.getLogger(__name__)


def _when(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _row(metadata: Mapping[str, Any]) -> Optional[MyKeyRecord]:
    key_id, fingerprint, node = metadata.get("id"), metadata.get("fingerprint"), metadata.get("node")
    if not key_id or not fingerprint or not node:
        return None
    return MyKeyRecord(
        key_id=str(key_id),
        fingerprint=str(fingerprint).lower(),
        owner_email=metadata.get("owner_email"),
        cluster_id=str(node),
        project=metadata.get("project"),
        name=metadata.get("name"),
        auth_level=metadata.get("auth_level"),
        created_at=_when(metadata.get("created_at")),
        expires_at=_when(metadata.get("expires_at")),
    )


async def record_created(db: Any, metadata: Mapping[str, Any]) -> None:
    """Register a key just issued."""
    await backfill(db, [metadata])


async def backfill(db: Any, roster: Iterable[Mapping[str, Any]]) -> int:
    """Register every key in `roster` not yet known; returns how many were added."""
    rows = [row for row in (_row(m) for m in roster) if row is not None]
    if not rows:
        return 0
    try:
        known = set(
            (await db.execute(select(MyKeyRecord.key_id).where(MyKeyRecord.key_id.in_([r.key_id for r in rows]))))
            .scalars()
            .all()
        )
        missing = [row for row in rows if row.key_id not in known]
        for row in missing:
            db.add(row)
        if missing:
            await db.commit()
        return len(missing)
    except Exception:  # noqa: BLE001 - the registry follows Vault; the next read retries
        logger.warning("Could not register My Keys in the audit registry", exc_info=True)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


async def record_deleted(db: Any, key_id: str, metadata: Optional[Mapping[str, Any]], deleted_by: Optional[str]) -> None:
    """Mark a key deleted, keeping its owner and cluster so its history stays readable."""
    try:
        row = await db.get(MyKeyRecord, key_id)
        if row is None and metadata is not None:
            row = _row(metadata)
            if row is not None:
                db.add(row)
        if row is None:
            return
        row.deleted_at = datetime.now(timezone.utc)
        row.deleted_by = deleted_by
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("Could not mark key %s deleted in the audit registry", key_id, exc_info=True)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
