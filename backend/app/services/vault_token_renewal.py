"""The dashboard keeps its own Vault token alive, and says so when it cannot.

A periodic token (`-period=720h`) lives as long as it is renewed within its period, so one worker
(Redis lease) renews it once less than half the period is left: the same rule the Full installer's
host watchdog uses, and renewing twice is harmless when both run. A token that cannot be renewed,
or not past its maximum lifetime (`-ttl=0` gets Vault's 768h), raises `vault.token_expiring` a
week ahead with the date and the fix. A root token never expires and is left alone.

Every worker also turns the token probe's transitions (core/vault_token.py) into the
`vault.token_refused` event, so a dead token is reported wherever it is first noticed.

important: the renew-self answer carries `auth.client_token` (the token itself). Only
`auth.lease_duration` is read from it; the body is never logged or kept.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from app.core import vault_token
from app.core.config import get_settings
from app.services import ops_events
from app.services.collector_leadership import CollectorLease

logger = logging.getLogger(__name__)

LEASE_KEY = "orcastra:vault:token_renewal_lease"
WARN_SECONDS = 7 * 24 * 3600
RETRY_SECONDS = 300.0
MIN_SECONDS = 30.0
_TIMEOUT = (2.0, 5.0)

_task: Optional[asyncio.Task] = None


def _duration(seconds: int) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours = rest // 3600
    if days:
        return f"{days} days" if not hours else f"{days} days {hours} hours"
    return f"{hours} hours" if hours else f"{max(1, rest // 60)} minutes"


def _expires_on(ttl_seconds: int, now: float) -> str:
    return (datetime.fromtimestamp(now, timezone.utc) + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M UTC")


def _renew(addr: str, token: str) -> tuple[Optional[int], Optional[int]]:
    """POST renew-self. (HTTP status or None when Vault did not answer, the new lease in seconds)."""
    try:
        response = requests.post(
            f"{addr}/v1/auth/token/renew-self", headers={"X-Vault-Token": token}, json={},
            timeout=_TIMEOUT, allow_redirects=False,
        )
    except requests.RequestException:
        return None, None
    try:
        if response.status_code != 200:
            return response.status_code, None
        try:
            auth = (response.json() or {}).get("auth") or {}
        except ValueError:
            return response.status_code, None
        lease = auth.get("lease_duration")
        return 200, lease if isinstance(lease, int) and not isinstance(lease, bool) else None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _next_check(interval: float, ttl_seconds: Optional[int]) -> float:
    if not ttl_seconds:
        return interval
    return max(MIN_SECONDS, min(interval, ttl_seconds / 3))


def _expiring(ttl_seconds: int, now: float, *, why: str) -> None:
    ops_events.fire("vault.token_expiring", facts={
        "expire_time": _expires_on(ttl_seconds, now), "ttl_seconds": ttl_seconds, "why": why,
    })


def run_once(*, settings=None, now: Optional[float] = None) -> float:
    """One check of the token, renewing it when due. Returns seconds until the next. Blocking."""
    settings = settings or get_settings()
    interval = float(getattr(settings, "vault_token_renew_check_seconds", 3600))
    if not (settings.vault_enabled and settings.vault_addr and settings.vault_token):
        return interval
    if not getattr(settings, "vault_token_renew_enabled", True):
        return interval
    now = time.time() if now is None else now
    token = vault_token.status(max_age=0, settings=settings)
    if token.state == "refused":
        # The probe listener reports it; nothing can be renewed with a dead token.
        return MIN_SECONDS * 2
    if token.state == "unknown":
        return RETRY_SECONDS
    if token.state != "ok":
        return interval
    ttl = token.ttl_seconds or 0
    if token.is_root or ttl == 0:
        ops_events.resolve("vault.token_expiring")
        return interval

    period = token.period_seconds or 0
    due_below = period / 2 if period else WARN_SECONDS
    if ttl >= due_below:
        ops_events.resolve("vault.token_renewal_failed")
        if ttl >= WARN_SECONDS:
            ops_events.resolve("vault.token_expiring")
        return _next_check(interval, ttl - due_below)
    if not token.renewable:
        if ttl < WARN_SECONDS:
            _expiring(ttl, now, why="not renewable")
        return _next_check(interval, ttl)

    status, lease = _renew(settings.vault_addr.rstrip("/"), settings.vault_token)
    if status == 200 and lease:
        vault_token.invalidate()
        ops_events.resolve("vault.token_renewal_failed")
        ops_events.record("vault.token_renewed", facts={"lease": _duration(lease), "ttl_seconds": lease})
        logger.info("Renewed the dashboard's Vault token (lease now %s)", _duration(lease))
        # Capped: Vault would not extend it past its maximum lifetime.
        if not period and lease < WARN_SECONDS:
            _expiring(lease, now, why="at its maximum lifetime")
        elif lease >= WARN_SECONDS:
            ops_events.resolve("vault.token_expiring")
        return _next_check(interval, lease)
    if status == 403:
        # A token that still looks itself up but may not renew lacks renew in its policy.
        again = vault_token.status(max_age=0, settings=settings)
        if again.state == "ok":
            _expiring(ttl, now, why="its policy does not allow renew-self")
            return _next_check(interval, ttl)
        return MIN_SECONDS * 2
    reason = "Vault did not answer" if status is None else f"Vault answered HTTP {status}"
    logger.warning("Could not renew the dashboard's Vault token: %s", reason)
    ops_events.fire("vault.token_renewal_failed", facts={"reason": reason})
    return min(RETRY_SECONDS, _next_check(interval, ttl))


def _on_token_change(before, after) -> None:
    if after.state == "refused":
        ops_events.fire("vault.token_refused")
    elif after.state == "ok" and before is not None and before.state == "refused":
        ops_events.resolve("vault.token_refused")


def _shared_redis():
    from app.core.cache import get_cache_manager

    return getattr(get_cache_manager(), "redis_client", None)


async def _loop() -> None:
    settings = get_settings()
    interval = float(settings.vault_token_renew_check_seconds)
    lease = CollectorLease(LEASE_KEY, int(interval * 2 + 60), _shared_redis)
    # Jittered so workers started together do not claim in lockstep.
    await asyncio.sleep(random.uniform(5.0, 30.0))
    try:
        while True:
            delay = interval
            try:
                if await asyncio.to_thread(lease.claim):
                    delay = await asyncio.to_thread(run_once)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a blip must not end the loop
                logger.warning("Vault token renewal pass failed", exc_info=True)
                delay = RETRY_SECONDS
            await asyncio.sleep(delay)
    finally:
        try:
            lease.release()
        except Exception:  # noqa: BLE001
            pass


def start() -> None:
    """Every worker reports token transitions; one at a time renews (lease)."""
    global _task
    vault_token.on_change(_on_token_change)
    settings = get_settings()
    if not (settings.vault_enabled and settings.vault_token and settings.vault_token_renew_enabled):
        return
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop())


async def stop() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
