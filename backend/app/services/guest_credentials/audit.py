"""What the activity trail keeps about a credential change: who, which account, which key, never a secret.

Details pass an allowlist. A password, a pasted key's text or a guest's output has no key to
travel under, and the names avoid the words the activity logger redacts (`password`, `secret`,
`token`, `credential`), so what is recorded stays readable instead of turning into `[REDACTED]`.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional, Tuple

from app.core.auth import AuthenticatedUser
from app.services.instance_files import audit as file_audit

ACTIONS = (
    "guest.account.view",
    "guest.account.view.granted",
    "guest.account.create",
    "guest.account.sudo",
    "guest.password.set",
    "guest.password.lock",
    "guest.authorized_key.add",
    "guest.authorized_key.remove",
)
ALLOWED_DETAILS = frozenset({
    "user", "uid", "kind", "sudo", "must_change", "fingerprint", "key_type", "bits", "steps",
    "script", "outcome", "route", "break_glass_grant_id", "change", "removed",
})
_VIEW_WINDOW_SECONDS = 900.0
_MAX_VIEW_KEYS = 10_000
_viewed: Dict[Tuple[str, str, str, str], float] = {}


def allowed(details: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in details.items() if key in ALLOWED_DETAILS}


@asynccontextmanager
async def audited(
    action: str, *, host: str, project: str, instance: str, details: Dict[str, Any]
) -> AsyncIterator[Dict[str, Any]]:
    """One record per write request, refusals included; the body adds what it learns as it goes."""
    record: Dict[str, Any] = {}
    record.update(allowed(details))
    async with file_audit.audited(action, host=host, project=project, instance=instance, details={}) as kept:
        try:
            yield record
        finally:
            kept.update(allowed(record))


def _should_record_view(user: Optional[AuthenticatedUser], host: str, project: str, instance: str) -> bool:
    key = (user.user_id if user else "anonymous", host, project, instance)
    now = time.monotonic()
    if len(_viewed) >= _MAX_VIEW_KEYS:
        _viewed.clear()
    last = _viewed.get(key)
    if last is not None and now - last < _VIEW_WINDOW_SECONDS:
        return False
    _viewed[key] = now
    return True


async def record_view(
    user: Optional[AuthenticatedUser], host: str, project: str, instance: str, grant_id: Optional[int], route: str
) -> None:
    # important: under a break-glass grant every read is recorded and ranks high. An Administrator
    # looking at a tenant's guest accounts is what that grant promises to show.
    if grant_id is not None:
        await file_audit.write(
            "guest.account.view.granted", host=host, project=project, instance=instance, result="success",
            details={"route": route, "break_glass_grant_id": grant_id},
        )
    elif _should_record_view(user, host, project, instance):
        await file_audit.write(
            "guest.account.view", host=host, project=project, instance=instance, result="success",
            details={"route": route},
        )
