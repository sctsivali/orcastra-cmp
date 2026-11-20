"""Deployment editions: the seams another packaging of this product plugs into.

The Full edition is the default and registers nothing, so every seam falls through to the
behaviour Full always had. Another edition is a package under ``app/editions/`` named by the
``ORCASTRA_EDITION`` environment variable; it exposes a module-level ``EDITION`` built from the
dataclass below. The package is not part of this tree: an edition that names a missing package
refuses to start rather than silently running as Full.

Each hook is read where Full already makes the matching decision, so a hook left at ``None``
costs one attribute read:

- ``token_verifier``: verifies an access token instead of the identity provider's JWKS
  (``core/auth.py``) and admits a ticket-opened socket (``core/ws_auth.py``).
- ``identity_directory``: builds the object ``get_authentik_sync()`` returns (role reads, role
  writes, the per-minute socket re-check, a key creator's role).
- ``authority_verifier``: builds Migration Center's actor verifier (``migrations/authority.py``).
- ``db_reconcilers``: recognise an unstamped database shape before the bootstrap refuses it.
- ``host_health_checks``: replace a Host Health check by id, keeping its id and place.
- ``blocked_path_prefixes``: API paths of Full features the edition does not offer; they answer
  404 before any route runs (included routers cannot be removed after mounting).
- ``validate_settings``, ``configure_app``, ``on_startup``, ``on_shutdown``, ``health_fields``:
  the application factory, lifespan and public health answer (``main.py``). ``configure_app``
  adds routes; it cannot replace or remove a Full route.

``ORCASTRA_EDITION`` is read from the process environment, not from ``.env`` through Settings: the
database bootstrap reads it before any settings exist. Compose files set it explicitly.
"""

from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional, Protocol, Set, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module import-cycle free
    from starlette.requests import HTTPConnection
    from starlette.websockets import WebSocket

    from app.core.auth import AuthenticatedUser
    from app.core.config import Settings

FULL = "full"
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class TokenVerifier(Protocol):
    """Verifies the bearer tokens of an edition that does not sign in through the IdP."""

    def configured(self, settings: "Settings") -> bool:
        """Whether this server can verify tokens at all; False answers every request 503."""

    async def verify(self, token: str, settings: "Settings", connection: "HTTPConnection") -> "AuthenticatedUser":
        """The caller of a request or socket, or an HTTPException (401, 503)."""

    async def admit_socket(self, user: "AuthenticatedUser", websocket: "WebSocket") -> Optional[str]:
        """None to admit a socket opened with a redeemed ticket, else the refusal reason token."""


# (live table names, whether alembic_version has rows, database URL) -> handled. A reconciler
# returns False for a shape it does not recognise and leaves the decision to the bootstrap.
DbReconciler = Callable[[Set[str], bool, str], bool]


@dataclass(frozen=True)
class Edition:
    name: str = FULL
    token_verifier: Optional[TokenVerifier] = None
    identity_directory: Optional[Callable[[], Any]] = None
    authority_verifier: Optional[Callable[[Any], Any]] = None
    db_reconcilers: Tuple[DbReconciler, ...] = ()
    host_health_checks: Dict[str, Tuple[str, Callable[..., Any]]] = field(default_factory=dict)
    blocked_path_prefixes: Tuple[str, ...] = ()
    validate_settings: Optional[Callable[["Settings"], None]] = None
    configure_app: Optional[Callable[[Any, "Settings"], None]] = None
    on_startup: Optional[Callable[[Any], Awaitable[None]]] = None
    on_shutdown: Optional[Callable[[Any], Awaitable[None]]] = None
    health_fields: Optional[Callable[[], Dict[str, Any]]] = None

    def __post_init__(self) -> None:
        # important: an edition that verifies its own tokens has no Authentik behind it. Left to
        # Authentik, the directory answers "not configured" for every account, which the open
        # terminal re-check reads as "keep going": a demoted or disabled user would keep a shell.
        if self.token_verifier is not None and (self.identity_directory is None or self.authority_verifier is None):
            raise RuntimeError(
                f"edition {self.name!r} verifies its own tokens and must also supply "
                "identity_directory and authority_verifier"
            )
        for prefix in self.blocked_path_prefixes:
            if not prefix.startswith("/") or prefix.endswith("/") or prefix in ("", "/health"):
                raise RuntimeError(f"edition {self.name!r} blocks an invalid path prefix {prefix!r}")


_FULL_EDITION = Edition()
_current: Optional[Edition] = None


def _load(name: str) -> Edition:
    name = (name or FULL).strip().lower()
    if name == FULL:
        return _FULL_EDITION
    if not _NAME.match(name):
        raise RuntimeError(f"ORCASTRA_EDITION={name!r} is not a valid edition name")
    try:
        module = importlib.import_module(f"app.editions.{name}")
    except ModuleNotFoundError as exc:
        # important: refuse instead of falling back to Full. An operator who asked for another
        # edition gets its sign-in and data rules or nothing, never Full's by accident.
        raise RuntimeError(f"ORCASTRA_EDITION={name!r} names an edition this build does not ship") from exc
    edition = getattr(module, "EDITION", None)
    if not isinstance(edition, Edition):
        raise RuntimeError(f"app.editions.{name} does not define an EDITION")
    return edition


def current_edition() -> Edition:
    """The edition this process runs, loaded once from ORCASTRA_EDITION."""
    global _current
    if _current is None:
        _current = _load(os.getenv("ORCASTRA_EDITION", FULL))
    return _current


def use_edition(edition: Optional[Edition]) -> None:
    """Pin the edition (tests); None reloads it from the environment on next use."""
    global _current
    _current = edition
