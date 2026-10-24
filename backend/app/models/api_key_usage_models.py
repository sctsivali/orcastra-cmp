"""When an external API key was last used, kept apart from the key's Vault document.

important: the hot path used to rewrite the whole key document to record this, from a cached
read and without the key's lock, so it could put back a grant a cleanup had just removed, an old
`is_revoked`, or old capabilities. A row here is the only thing a request writes.
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
