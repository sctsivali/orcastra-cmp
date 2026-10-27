"""Whether Vault itself is sealed, uninitialized or unreachable, asked of Vault directly.

Issue #15: after a reboot Vault comes back sealed, every cluster read fails, and the dashboard
looked like a deployment with no clusters. vault_status records whether the last registry LIST
answered; nothing asked Vault *why* it did not. `sys/seal-status` answers that, unauthenticated
and even while sealed, so the dashboard can say "Vault is sealed" instead of "no clusters".

important: a bare requests.get, not VaultClient.session. That session retries 5xx and connect
errors twice with backoff, so one probe of a dead Vault took about ten seconds. This one costs
one round trip with a 2 s connect and 3 s read budget.

Per process and lock-guarded like vault_status: the lock is held across the HTTP call, so a burst
of callers shares one probe (single flight) and the answer is kept for PROBE_TTL_SECONDS.
"""

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Literal, Optional

import requests

from app.core import vault_status
from app.core.config import get_settings

logger = logging.getLogger(__name__)

ProbeState = Literal["ok", "sealed", "uninitialized", "unreachable", "not_configured"]

PROBE_TTL_SECONDS = 10.0
# A forced probe (the notice's "Check now") still reuses an answer younger than this.
FORCE_FLOOR_SECONDS = 2.0
_TIMEOUT = (2.0, 3.0)


@dataclass(frozen=True)
class SealFacts:
    """The parts of `sys/seal-status` an Administrator may see. Every field is public in Vault itself
    (the endpoint is unauthenticated); none of them is key material."""
    seal_type: Optional[str] = None
    threshold: Optional[int] = None
    shares: Optional[int] = None
    progress: Optional[int] = None
    # The current unseal attempt's id; empty while no key has been entered. Not a secret: it is what
    # lets several key holders tell they are adding to the same attempt.
    nonce: str = ""
    version: Optional[str] = None
    storage_type: Optional[str] = None
    recovery_seal: Optional[bool] = None


@dataclass(frozen=True)
class SealProbe:
    state: ProbeState
    checked_at: float
    # A class name and a short reason, scrubbed of any address; None when Vault answered ok.
    reason: Optional[str] = None
    facts: Optional[SealFacts] = None


def _int(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def facts_from(body: dict) -> SealFacts:
    """The admin-safe subset of a `sys/seal-status` (or `sys/unseal`) answer."""
    recovery = body.get("recovery_seal")
    return SealFacts(
        seal_type=_str(body.get("type")),
        threshold=_int(body.get("t")),
        shares=_int(body.get("n")),
        progress=_int(body.get("progress")),
        nonce=body.get("nonce") if isinstance(body.get("nonce"), str) else "",
        version=_str(body.get("version")),
        storage_type=_str(body.get("storage_type")),
        recovery_seal=recovery if isinstance(recovery, bool) else None,
    )


_lock = threading.Lock()
_last: Optional[SealProbe] = None


def _ask(addr: str) -> SealProbe:
    now = time.time()
    try:
        response = requests.get(f"{addr.rstrip('/')}/v1/sys/seal-status", timeout=_TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        return SealProbe("unreachable", now, vault_status.public_error(f"{type(exc).__name__}: {exc}"))
    if response.status_code != 200:
        return SealProbe("unreachable", now, f"seal-status answered HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        return SealProbe("unreachable", now, "seal-status answered a body that is not JSON")
    if not isinstance(body, dict) or not isinstance(body.get("sealed"), bool):
        return SealProbe("unreachable", now, "seal-status answered an unexpected body")
    facts = facts_from(body)
    if body.get("initialized") is False:
        return SealProbe("uninitialized", now, "Vault reports it is not initialized", facts)
    if body["sealed"]:
        return SealProbe("sealed", now, "Vault reports it is sealed", facts)
    return SealProbe("ok", now, None, facts)


def seal_probe(force: bool = False) -> SealProbe:
    """Vault's own answer, at most PROBE_TTL_SECONDS old (FORCE_FLOOR_SECONDS when forced).

    Blocking: call it off the event loop.
    """
    global _last
    settings = get_settings()
    if not settings.vault_enabled or not settings.vault_addr:
        return SealProbe("not_configured", time.time(), "Vault integration is not enabled")
    with _lock:
        age = time.time() - _last.checked_at if _last is not None else None
        if age is not None and age < (FORCE_FLOOR_SECONDS if force else PROBE_TTL_SECONDS):
            return _last
        # Stamped when the answer arrived, not when it was asked: a 3 s timeout stamped at the
        # start already looked older than the forced floor, so a looped Check now kept the lock.
        probe = replace(_ask(settings.vault_addr), checked_at=time.time())
        if _last is None or _last.state != probe.state:
            # One line per change, not per probe: the poll would otherwise log every 10 seconds.
            if probe.state == "ok":
                logger.info("Vault seal probe: ok")
            else:
                logger.error("Vault seal probe: %s (%s)", probe.state, probe.reason)
        _last = probe
        return probe


def peek() -> Optional[SealProbe]:
    """The last answer, without asking. For /health, which must never wait on Vault.

    important: no lock. seal_probe holds _lock across the network call, and /health runs on the
    event loop, so taking it here stalled the whole worker for the length of a probe of a dead
    Vault. Reading one reference is atomic, and the value is an immutable dataclass.
    """
    return _last


def ask_now() -> SealProbe:
    """Vault's answer right now, bypassing the cache, and kept for later callers.

    For the unseal route, which must compare the attempt it is adding to with the one Vault holds;
    a two-second-old nonce could belong to an attempt someone else has since reset. Blocking.
    """
    settings = get_settings()
    if not settings.vault_enabled or not settings.vault_addr:
        return SealProbe("not_configured", time.time(), "Vault integration is not enabled")
    probe = replace(_ask(settings.vault_addr), checked_at=time.time())
    record(probe)
    return probe


def record(probe: SealProbe) -> None:
    """Keep an answer Vault gave outside seal_probe (the unseal call returns the same status).

    note: per process. Another worker keeps its own answer for at most PROBE_TTL_SECONDS, which is why
    the console re-reads the status with `fresh` after an unseal instead of trusting one poll.
    """
    global _last
    with _lock:
        if _last is None or _last.state != probe.state:
            logger.info("Vault seal probe: %s (recorded)", probe.state)
        _last = probe


def reset() -> None:
    """Test hook. Not called by application code."""
    global _last
    with _lock:
        _last = None
