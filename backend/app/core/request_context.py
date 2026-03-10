"""
Request Context Module
Provides context variables for request-scoped data like request_id.
Uses Python's contextvars for thread-safe async context.
"""

import uuid
from contextvars import ContextVar
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class RequestContext:
    """
    Request context data that flows through the request lifecycle.
    """
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    user_id: Optional[str] = None
    user_role: Optional[str] = None  # admin | partner | tenant
    user_groups: list = field(default_factory=list)
    user_orgs: list = field(default_factory=list)  # org names/slugs user belongs to
    session_id: Optional[str] = None
    client_ip: Optional[str] = None
    user_agent: Optional[str] = None
    origin: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    # backward-compatible alias - user_type is always identical to user_role
    @property
    def user_type(self) -> Optional[str]:
        return self.user_role

    @user_type.setter
    def user_type(self, value: Optional[str]) -> None:
        self.user_role = value

    def to_dict(self) -> Dict[str, Any]:
        """Convert context to dictionary for logging."""
        return {
            "request_id": self.request_id,
            "user_id": self.user_id,
            "role": self.user_role,
            "user_groups": self.user_groups,
            "user_orgs": self.user_orgs,
            "session_id": self.session_id,
            "client_ip": self.client_ip,
        }

    def get_actor(self) -> Dict[str, Any]:
        """Get actor info for audit logs."""
        return {
            "user_id": self.user_id,
            "user_type": self.user_role,
            "role": self.user_role,
            "groups": self.user_groups,
            "organizations": self.user_orgs,
            "session_id": self.session_id,
        }


# Context variable to store request context
_request_context: ContextVar[Optional[RequestContext]] = ContextVar(
    "request_context", default=None
)


def get_request_context() -> Optional[RequestContext]:
    """Get the current request context."""
    return _request_context.get()


def set_request_context(context: RequestContext) -> None:
    """Set the current request context."""
    _request_context.set(context)


def get_request_id() -> Optional[str]:
    """Get the current request ID."""
    ctx = get_request_context()
    return ctx.request_id if ctx else None


def get_current_user_id() -> Optional[str]:
    """Get the current user ID from context."""
    ctx = get_request_context()
    return ctx.user_id if ctx else None


def generate_request_id() -> str:
    """Generate a new request ID."""
    return str(uuid.uuid4())


def generate_event_id() -> str:
    """Generate a new event ID for audit logs."""
    return f"evt_{uuid.uuid4()}"
