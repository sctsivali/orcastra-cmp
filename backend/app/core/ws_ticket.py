"""Single-use tickets that open a WebSocket in place of an access token in its URL.

important: a browser cannot set headers on a WebSocket upgrade, so every socket here carried the
caller's access token in its query string, where proxies, access logs and crash reports keep
URLs. A ticket is minted over an authenticated POST, lives 30 seconds, opens one socket of one
kind for one target, and is deleted when it is redeemed. The store keeps only its SHA-256, so
neither a key listing nor a log line can replay one.

note: a ticket carries the identity of the token it was minted with, and that token's expiry as
the session's. Connect-time authorization still runs on every socket; the ticket replaces only
how the caller proves who they are.
"""

import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Literal, Mapping, Optional, Tuple

from app.core.auth import AuthenticatedUser

WsKind = Literal["monitoring", "events", "tasks", "terminal", "console"]
WS_KINDS: Tuple[str, ...] = ("monitoring", "events", "tasks", "terminal", "console")
# Feeds close when the session ends and reconnect with a fresh ticket; interactive sockets
# re-authorize while open instead, so a shell is not cut mid-command.
FEED_KINDS = frozenset({"monitoring", "events", "tasks"})
INTERACTIVE_KINDS = frozenset({"terminal", "console"})
TICKET_TTL_SECONDS = 30
# A SPICE display opens one socket per channel: main, display, inputs and cursor.
SPICE_CHANNELS = 4
# Claims kept from the access token: what RBAC and the migration authority read, nothing else.
_KEPT_CLAIMS = ("iss", "sub", "aud", "exp", "iat", "email", "name", "preferred_username", "groups")


@dataclass(frozen=True)
class WsTarget:
    """What one socket may open: its Cluster, project, instance and console type, as relevant."""

    host: Optional[str] = None
    project: Optional[str] = None
    instance: Optional[str] = None
    console_type: Optional[str] = None


@dataclass(frozen=True)
class TicketClaims:
    kind: str
    target: WsTarget
    user_id: str
    email: Optional[str]
    name: Optional[str]
    groups: Tuple[str, ...]
    roles: Tuple[str, ...]
    claims: Dict[str, Any]
    session_exp: Optional[float]
    minted_at: float
    # One random id shared by the tickets of one SPICE display, so its channels can find the one
    # LXD operation they share (services/spice_session.py). Signed with the rest of the claims.
    group: Optional[str] = None

    def to_json(self) -> str:
        body = asdict(self)
        body["groups"], body["roles"] = list(self.groups), list(self.roles)
        return json.dumps(body, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "TicketClaims":
        body = json.loads(raw)
        target = body.get("target") or {}
        exp = body.get("session_exp")
        return cls(
            kind=str(body["kind"]),
            target=WsTarget(**{key: target.get(key) for key in ("host", "project", "instance", "console_type")}),
            user_id=str(body["user_id"]),
            email=body.get("email"),
            name=body.get("name"),
            groups=tuple(str(group) for group in body.get("groups") or ()),
            roles=tuple(str(role) for role in body.get("roles") or ()),
            claims=dict(body.get("claims") or {}),
            session_exp=float(exp) if isinstance(exp, (int, float)) else None,
            minted_at=float(body["minted_at"]),
            group=str(body["group"]) if body.get("group") else None,
        )

    def user(self) -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id=self.user_id,
            email=self.email,
            name=self.name,
            groups=list(self.groups),
            roles=list(self.roles),
            raw_claims=dict(self.claims),
            session_exp=self.session_exp,
        )


def claims_for(kind: str, target: WsTarget, user: Optional[AuthenticatedUser]) -> TicketClaims:
    """The claims a ticket of `kind` for `target` carries for `user` (None when auth is off)."""
    raw: Mapping[str, Any] = user.raw_claims if user is not None else {}
    return TicketClaims(
        kind=kind,
        target=target,
        user_id=user.user_id if user is not None else "anonymous",
        email=user.email if user is not None else None,
        name=user.name if user is not None else None,
        groups=tuple(user.groups) if user is not None else (),
        roles=tuple(user.roles) if user is not None else (),
        claims={key: raw[key] for key in _KEPT_CLAIMS if key in raw},
        session_exp=user.session_exp if user is not None else None,
        minted_at=time.time(),
    )


def new_ticket() -> str:
    return secrets.token_urlsafe(32)


def ticket_digest(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def socket_target(kind: str, path_params: Mapping[str, Any], query: Mapping[str, Any]) -> WsTarget:
    """The target a socket of `kind` is asking to open, from its path and query."""
    if kind == "events":
        return WsTarget(host=query.get("host"))
    if kind in INTERACTIVE_KINDS:
        return WsTarget(
            host=query.get("host"),
            project=query.get("project") or "default",
            instance=path_params.get("instance_name"),
            console_type=(query.get("console_type") or "vga") if kind == "console" else None,
        )
    return WsTarget()
