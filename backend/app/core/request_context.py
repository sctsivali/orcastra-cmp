"""Request-scoped data (request id, caller, client) in a ContextVar, read by logging and audit."""

import uuid
from contextvars import ContextVar
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class RequestContext:
    """Request data that flows through the request lifecycle."""
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    user_id: Optional[str] = None
    user_role: Optional[str] = None  # admin | partner | tenant
    user_groups: list = field(default_factory=list)
    user_orgs: list = field(default_factory=list)  # names of orgs the user belongs to or created
    session_id: Optional[str] = None
    client_ip: Optional[str] = None
    user_agent: Optional[str] = None
    origin: Optional[str] = None
    # The HTTP method and path, so a scope check can tell a change from a read (core/org_scope.py).
    method: Optional[str] = None
    path: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    # Alias of user_role, kept for existing readers.
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

    def client(self) -> Dict[str, Any]:
        """Where the request came from, as the access log names it, for the audit record.

        `ip_source` says which header the address came from ("direct" when none was trusted), and
        `proxy_trusted` whether the peer was a proxy allowed to say. A "direct" address behind a
        trusted proxy is the proxy's own address. The user agent is the browser's own claim,
        capped and stripped of control characters.
        """
        agent = "".join(ch for ch in (self.user_agent or "") if ch.isprintable())[:256]
        return {
            "ip": self.client_ip,
            "ip_source": self.extra.get("client_ip_source", "direct"),
            "proxy_trusted": bool(self.extra.get("client_proxy_trusted", False)),
            "user_agent": agent or None,
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
            "client": self.client(),
        }


_request_context: ContextVar[Optional[RequestContext]] = ContextVar(
    "request_context", default=None
)


def get_request_context() -> Optional[RequestContext]:
    return _request_context.get()


def set_request_context(context: RequestContext) -> None:
    _request_context.set(context)


def get_request_id() -> Optional[str]:
    ctx = get_request_context()
    return ctx.request_id if ctx else None


def get_current_user_id() -> Optional[str]:
    ctx = get_request_context()
    return ctx.user_id if ctx else None


def generate_request_id() -> str:
    return str(uuid.uuid4())


def generate_event_id() -> str:
    """A new event id for an audit record."""
    return f"evt_{uuid.uuid4()}"
