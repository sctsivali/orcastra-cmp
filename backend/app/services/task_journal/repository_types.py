"""The value types and small helpers every task journal writer shares.

Kept apart from the SQL so the write path and the sweeps can import them without importing each
other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from app.core.error_sanitizer import sanitize_error

FOLLOWER_LOCK = (20330, 21578)
PRUNE_LOCK = (20330, 21579)

LOST_MESSAGE = "The cluster stopped reporting this operation"
STALE_MESSAGE = "The dashboard stopped observing this action"


@dataclass(frozen=True)
class OperationFrame:
    cluster_id: str
    operation_id: str
    operation_class: Optional[str]
    description: str
    status: str
    status_code: Optional[int]
    progress: Optional[str]
    error: Optional[str]
    may_cancel: bool
    project: str
    resource_type: Optional[str]
    resource_name: Optional[str]
    action: Optional[str]
    lxd_created_at: Optional[datetime]
    lxd_updated_at: Optional[datetime]
    metadata: Dict[str, Any]
    requestor: Optional[Dict[str, Any]]
    actor_kind: str = "system"
    source: str = "events"


@dataclass(frozen=True)
class ActorFields:
    actor_email: Optional[str]
    actor_role: Optional[str]
    actor_kind: str
    api_key_id: Optional[str]
    source: str
    request_id: Optional[str]
    target_organization_id: Optional[int]
    target_organization_slug: Optional[str]


@dataclass(frozen=True)
class CorrelationKey:
    cluster_id: str
    project: str
    resource_type: str
    resource_name: str
    action: str


@dataclass(frozen=True)
class UpsertResult:
    id: str
    created: bool
    changed: bool


def clean_message(message: Optional[str]) -> Optional[str]:
    return sanitize_error(message) if message else None


def json_param(obj: Optional[Mapping[str, Any]]) -> str:
    return json.dumps(dict(obj or {}), default=str)


def returned_id(result: Any) -> Optional[str]:
    row = result.first()
    if row is None:
        return None
    return str(row[0])
