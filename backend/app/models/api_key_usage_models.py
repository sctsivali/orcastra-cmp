"""When an external API key was last used, kept apart from the key's Vault document.

important: a request records its use here and nowhere else. Rewriting the key document on the
hot path, from a cached read and without the key's lock, could put back a grant a cleanup had
just removed, an old `is_revoked`, or old capabilities.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApiKeyUsage(Base):
    __tablename__ = "api_key_usage"

    key_id = Column(String(255), primary_key=True)
    last_used_at = Column(DateTime(timezone=True), nullable=False, default=_now)
