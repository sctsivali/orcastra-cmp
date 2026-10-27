"""The console toolbar's actions, recorded: a key combination sent to a guest, a screenshot taken.

The keys themselves travel over the SPICE socket, which the backend relays without reading. So the
browser reports each toolbar action here, after the fact and fire-and-forget, and this route writes
the audit record. Typing on a real keyboard is not recorded, and the documentation says so.

Only a caller who may open the console of this instance can record against it, and a caller is held
to a per-minute budget so the route cannot be used to flood the audit trail.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field, model_validator

from app.core.dependencies import AuthenticatedUser, require_tenant
from app.core.lxd_names import LXD_NAME_PATTERN
from app.core.org_scope import org_scope_dependency
from app.middleware.security import _shared_allow
from app.routers.console import _verify_instance_access
from app.services.activity_logger import get_activity_logger

router = APIRouter(prefix="/console", tags=["Console"])

# The named combinations the toolbar offers (frontend lib/spice/keys.ts COMBOS). Kept in step by
# tests/test_console_actions_are_audited.py.
COMBO_IDS = frozenset({
    "ctrl-alt-del", "lock", "switch-user", "task-manager", "alt-tab", "alt-f4", "win", "win-r",
    "print-screen", *(f"ctrl-alt-f{n}" for n in range(1, 13)),
})
_KEY = r"(?:[a-z0-9]|f(?:[1-9]|1[0-2])|escape|tab|enter|space|backspace|delete|insert|home|end|pageup|pagedown|up|down|left|right|printscreen)"
# A custom combination: up to four distinct modifiers in a fixed order, then one key.
CUSTOM_COMBO = re.compile(rf"^(?:ctrl\+)?(?:alt\+)?(?:shift\+)?(?:win\+)?{_KEY}$")

PER_MINUTE = 120
_local: Dict[str, List[float]] = {}
_local_lock = threading.Lock()


class ConsoleActionBody(BaseModel):
    host: str = Field(..., min_length=1, max_length=255)
    project: str = Field("default", min_length=1, max_length=255)
    action: Literal["keys.send", "screenshot.take"]
    combo: Optional[str] = Field(None, max_length=64)

    @model_validator(mode="after")
    def _combo_matches_action(self) -> "ConsoleActionBody":
        if self.action == "screenshot.take":
            if self.combo is not None:
                raise ValueError("a screenshot carries no key combination")
            return self
        combo = (self.combo or "").strip().lower()
        if combo not in COMBO_IDS and not CUSTOM_COMBO.match(combo):
            raise ValueError("combo must be a toolbar combination or modifiers+key, for example ctrl+alt+f2")
        self.combo = combo
        return self


def _within_budget(user_id: str) -> bool:
    shared = _shared_allow(f"console-actions:{user_id}", PER_MINUTE, 60)
    if shared is not None:
        return shared
    now = time.monotonic()
    with _local_lock:
        recent = [t for t in _local.get(user_id, []) if now - t < 60]
        recent.append(now)
        _local[user_id] = recent
        return len(recent) <= PER_MINUTE


@router.post(
    "/{instance_name}/actions",
    status_code=204,
    summary="Record a console toolbar action",
    dependencies=[Depends(org_scope_dependency)],
)
async def record_console_action(
    body: ConsoleActionBody,
    instance_name: str = Path(..., pattern=LXD_NAME_PATTERN),
    user: AuthenticatedUser = Depends(require_tenant()),
) -> None:
    project = _verify_instance_access(body.host, body.project, instance_name, "console")
    if not _within_budget(user.user_id or user.email or "anonymous"):
        raise HTTPException(status_code=429, detail="Too many console actions in a minute.", headers={"Retry-After": "60"})
    get_activity_logger().log_activity(
        action="console.keys.send" if body.action == "keys.send" else "console.screenshot.take",
        target_type="instance",
        target_id=instance_name,
        target_host=body.host,
        target_project=project,
        details={"combo": body.combo} if body.combo else None,
    )
