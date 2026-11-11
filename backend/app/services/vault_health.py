"""What the dashboard's Vault notice shows: Vault's own state, the token's, then the registry's.

Vault can be unsealed and still refuse the cluster registry (a token whose policy denies the LIST,
or a token that expired or was revoked), which reads the same to an operator as a sealed one. So
the seal probe's answer comes first, then the token's own lookup ("token_refused": renew or
replace VAULT_TOKEN), and the registry's last: "unavailable" when Vault says it is fine but the
last registry read failed. After an unseal the registry state is only as fresh as the last read,
so one confirming read runs here when the last one failed, instead of holding the notice up until
the 30 s sweep.
"""

import threading
import time
from dataclasses import dataclass
from typing import Literal, Optional

from app.core import vault_probe, vault_status, vault_token

VaultState = Literal[
    "ok", "sealed", "uninitialized", "unreachable", "token_refused", "unavailable", "not_configured"
]


@dataclass(frozen=True)
class VaultHealth:
    state: VaultState
    checked_at: float
    registry_source: Optional[str]
    reason: Optional[str]
    registry_error: Optional[str]
    registry_checked_at: Optional[float]


# Its own spacing rather than vault_status.retry_due(). The registry's retry hold is set by the
# very failure the unseal just ended, so honouring it would keep the notice up after Vault has
# already said it is fine.
CONFIRM_INTERVAL_SECONDS = 5.0
_confirm_lock = threading.Lock()
_last_confirm_at = 0.0


def _confirm_due() -> bool:
    global _last_confirm_at
    with _confirm_lock:
        now = time.monotonic()
        if now - _last_confirm_at < CONFIRM_INTERVAL_SECONDS:
            return False
        _last_confirm_at = now
        return True


def resolve_vault_health(cert_manager, force: bool = False) -> VaultHealth:
    """Blocking (one Vault probe, at most one registry LIST): call it off the event loop."""
    probe = vault_probe.seal_probe(force=force)
    state: VaultState = probe.state
    reason = probe.reason
    if state == "ok":
        token = vault_token.status(max_age=vault_probe.FORCE_FLOOR_SECONDS if force else None)
        if token.state == "refused":
            state = "token_refused"
            reason = "Vault rejects the dashboard's token (expired or revoked)"
    if state == "ok" and vault_status.registry_readable() is False and _confirm_due():
        cert_manager.read_registry(use_cache=False)
    if state == "ok" and vault_status.registry_readable() is False:
        state = "unavailable"
    snapshot = vault_status.snapshot()
    return VaultHealth(
        state=state,
        checked_at=probe.checked_at,
        registry_source=snapshot.get("registry_source"),
        reason=reason,
        registry_error=snapshot.get("error"),
        registry_checked_at=snapshot.get("checked_at"),
    )


def reset() -> None:
    """Test hook. Not called by application code."""
    global _last_confirm_at
    with _confirm_lock:
        _last_confirm_at = 0.0
