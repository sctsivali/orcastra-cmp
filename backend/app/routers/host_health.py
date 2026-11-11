"""Host Health: the dashboard's own dependencies, and unsealing Vault from the console.

Administrators only, every route. The report names internal addresses, and an unseal is an act on
the platform rather than on any one organization's clusters. Interactive sign-in only: these routes
never take an integration API key.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Deque, Dict, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.core import vault_probe
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.config import get_settings
from app.core.dependencies import AuthenticatedUser, require_admin
from app.core.json_body import body_spec, read_json_body
from app.core.vault_probe import SealFacts, SealProbe
from app.services import host_health, vault_unseal
from app.services.host_health.models import HostHealthReport, localized

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["Host Health"], dependencies=[Depends(require_admin())])

# Two pools: the dialog polls the status every 2 s from every open browser, and a bound shared with
# the submit would let a slow Vault turn a key submission into "pool full" before it is sent.
_status_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vault_seal_status")
_unseal_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vault_unseal")
# Above the sum of the submit's socket budgets (probe 5 s, PUT 12 s, re-probe 5 s), so a slow Vault
# is answered and audited by the submit itself instead of being cut off as "timeout".
UNSEAL_DEADLINE_SECONDS = 30
UNSEAL_LIMIT = 20
UNSEAL_WINDOW_SECONDS = 600
_local_hits: Dict[str, Deque[float]] = {}
_local_lock = threading.Lock()
# INCR and EXPIRE in one step. Two calls could leave a counter with no expiry when the second one
# failed, and an Administrator locked out of unsealing for good during a Redis blip.
_COUNT = """
local count = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then redis.call('EXPIRE', KEYS[1], ARGV[1]); ttl = tonumber(ARGV[1]) end
return {count, ttl}
"""


class SealStatusResponse(BaseModel):
    state: Literal["ok", "sealed", "uninitialized", "unreachable", "not_configured"]
    seal_type: Optional[str] = None
    threshold: Optional[int] = None
    shares: Optional[int] = None
    progress: Optional[int] = None
    nonce: str = ""
    version: Optional[str] = None
    unseal_available: bool
    unseal_unavailable_reason: Optional[str] = None
    # The backend reaches Vault over plain HTTP: a share crosses that link unencrypted.
    insecure_transport: bool
    checked_at: float


class UnsealResponse(SealStatusResponse):
    outcome: vault_unseal.Outcome


class UnsealRequest(BaseModel):
    """Exactly one of `key` and `reset`. `nonce` is the attempt the caller last saw ("" when none)."""
    model_config = ConfigDict(extra="forbid")

    # SecretStr so a repr or log of the request never shows it; read_json_body keeps the 422 from
    # echoing it.
    key: Optional[SecretStr] = Field(default=None, max_length=vault_unseal.KEY_MAX_LENGTH * 2)
    reset: bool = False
    nonce: Optional[str] = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9-]*$")


def _status(probe: Optional[SealProbe]) -> SealStatusResponse:
    settings = get_settings()
    probe = probe or SealProbe("unreachable", time.time(), "no answer yet")
    facts = probe.facts or SealFacts()
    allowed, reason = vault_unseal.availability(settings, probe)
    return SealStatusResponse(
        state=probe.state, seal_type=facts.seal_type, threshold=facts.threshold, shares=facts.shares,
        progress=facts.progress, nonce=facts.nonce, version=facts.version,
        unseal_available=allowed, unseal_unavailable_reason=None if allowed else reason,
        insecure_transport=vault_unseal.insecure_transport(settings.vault_addr),
        checked_at=probe.checked_at,
    )


def _allow(user_id: str) -> Tuple[bool, int]:
    """Whether this Administrator may submit now, and the seconds until the window resets."""
    try:
        from app.core.cache import get_cache_manager

        client = get_cache_manager().redis_client
        if client is not None:
            count, ttl = client.register_script(_COUNT)(
                keys=[f"orcastra:ratelimit:vault_unseal:{user_id}"], args=[UNSEAL_WINDOW_SECONDS])
            return int(count) <= UNSEAL_LIMIT, max(1, int(ttl))
    except Exception as exc:  # noqa: BLE001 - fall back to this worker's own window
        logger.warning("Unseal rate limit store unavailable, counting in this worker: %s", type(exc).__name__)
    # important: without Redis this worker's own window keeps a bound instead of none.
    now = time.monotonic()
    with _local_lock:
        hits = _local_hits.setdefault(user_id, deque())
        while hits and now - hits[0] > UNSEAL_WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= UNSEAL_LIMIT:
            return False, max(1, int(UNSEAL_WINDOW_SECONDS - (now - hits[0])))
        hits.append(now)
        return True, UNSEAL_WINDOW_SECONDS


def _audit(action: str, result: str, details: Dict[str, object], error_code: Optional[str] = None) -> None:
    from app.services.activity_logger import get_activity_logger

    # important: an allowlist of what may be recorded. The share is never among the inputs here.
    allowed = {k: details[k] for k in ("outcome", "progress", "threshold", "sealed", "attempt") if k in details}
    get_activity_logger().log_activity(
        action=action, target_type="vault", target_id="seal", result=result,
        error_code=error_code, details=allowed,
    )


@router.get("/host-health", response_model=HostHealthReport)
async def get_host_health(response: Response, fresh: bool = False):
    """The dashboard's dependencies. `fresh` runs every check now instead of serving the last pass."""
    response.headers["Cache-Control"] = "no-store"
    return localized(await host_health.get_report(fresh=fresh))


@router.get("/vault/seal-status", response_model=SealStatusResponse)
async def get_seal_status(response: Response):
    """Vault's seal state and unseal progress, for the unseal dialog: Vault's answer now, never cached."""
    # important: do not use seal_probe(force=True). It reuses an answer up to two seconds old per
    # worker, so a poll landing on another worker can show the attempt as it stood before the last
    # key: progress goes backwards and a reset carries a stale nonce, which the unseal route refuses.
    response.headers["Cache-Control"] = "no-store"
    try:
        probe = await run_blocking(vault_probe.ask_now, timeout=10, label="vault.seal_status",
                                   executor=_status_executor, max_in_flight=4)
    except (BlockingTimeout, BlockingPoolFull) as exc:
        raise HTTPException(status_code=503, detail="The Vault status could not be read in time") from exc
    return _status(probe)


@router.post("/vault/unseal", response_model=UnsealResponse, openapi_extra=body_spec(UnsealRequest))
async def unseal_vault(request: Request, response: Response, user: AuthenticatedUser = Depends(require_admin())):
    """Submit one unseal key share (or reset the attempt) to the Vault at VAULT_ADDR.

    Allowed in read-only mode: it restores a dependency and changes nothing in the fleet.
    """
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    if not settings.vault_unseal_ui_enabled:
        raise HTTPException(status_code=403, detail={
            "error": "unseal_disabled",
            "message": "Unsealing from the dashboard is turned off (VAULT_UNSEAL_UI_ENABLED=false).",
        })
    if not settings.auth_enabled:
        raise HTTPException(status_code=403, detail={
            "error": "unseal_requires_sign_in",
            "message": "Unsealing from the dashboard requires sign-in (AUTH_ENABLED).",
        })
    body = await read_json_body(request, UnsealRequest)
    if (body.key is None) == (not body.reset):
        raise HTTPException(status_code=422, detail={
            "error": "invalid_request", "message": "Send either a key or reset, not both and not neither.",
        })
    key: Optional[str] = None
    if body.key is not None:
        try:
            key = vault_unseal.normalise_key(body.key.get_secret_value())
        except vault_unseal.UnsealError as exc:
            raise HTTPException(status_code=exc.status, detail={"error": exc.code, "message": exc.message}) from None
    allowed, retry_after = _allow(user.user_id)
    if not allowed:
        _audit("vault.unseal.reset" if body.reset else "vault.unseal.submit", "denied",
               {"outcome": "rate_limited"}, "rate_limited")
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry_after)}, detail={
            "error": "unseal_rate_limited",
            "message": f"Too many unseal submissions from this account. Try again in {max(1, round(retry_after / 60))}m.",
        })
    action = "vault.unseal.reset" if body.reset else "vault.unseal.submit"
    try:
        outcome = await run_blocking(
            vault_unseal.submit, settings, key=key, reset=body.reset, nonce=body.nonce,
            timeout=UNSEAL_DEADLINE_SECONDS, label="vault.unseal", executor=_unseal_executor, max_in_flight=2,
        )
    except BlockingPoolFull:
        _audit(action, "failure", {"outcome": "busy"}, "busy")
        raise HTTPException(status_code=503, headers={"Retry-After": "5"}, detail={
            "error": "unseal_busy",
            "message": "Another unseal request is still being processed. Nothing was submitted; try again in a moment.",
        }) from None
    except BlockingTimeout:
        _audit(action, "failure", {"outcome": "timeout"}, "timeout")
        raise HTTPException(status_code=503, detail={
            "error": "vault_unconfirmed",
            "message": "Vault did not answer in time. Check the progress before entering the key again.",
        }) from None
    except vault_unseal.UnsealError as exc:
        facts = (exc.probe.facts if exc.probe else None) or SealFacts()
        _audit(action, "failure", {"outcome": exc.code, "progress": facts.progress, "threshold": facts.threshold},
               exc.code)
        status = _status(exc.probe).model_dump()
        raise HTTPException(status_code=exc.status, detail={"error": exc.code, "message": exc.message, "status": status}) from None
    finally:
        key = None

    facts = outcome.probe.facts or SealFacts()
    _audit(action, "success", {"outcome": outcome.outcome, "progress": facts.progress,
                               "threshold": facts.threshold, "sealed": outcome.probe.state != "ok"})
    if outcome.outcome == "unsealed":
        _audit("vault.unsealed", "success", {"outcome": "unsealed", "threshold": facts.threshold})
        try:
            await host_health.mark_vault_unsealed()
        except Exception:  # noqa: BLE001 - the unseal happened; a stale report row must not undo the answer
            logger.warning("Host Health report not updated after the unseal", exc_info=True)
        host_health.refresh_soon()
    return UnsealResponse(**_status(outcome.probe).model_dump(), outcome=outcome.outcome)
