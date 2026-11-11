"""Whether Vault still honours the dashboard's own token, asked of Vault directly.

An expired or revoked token makes every Vault read answer 403 while Vault itself is unsealed and
healthy, so the seal probe (core/vault_probe.py) says "ok" and nothing else notices. Vault answers
403 just the same for a path the token's policy does not cover, and a caller can steer that path
(an API key id becomes a path segment), so a 403 alone never proves the store failed.
`lookup-self` asks about the token and nothing a caller chose; it is what decides.

important: a bare requests call, not VaultClient.session, whose retries would turn one probe of a
dead Vault into ten seconds. The response body is never logged, returned or cached as a whole: it
carries the token's id and accessor. Only the lifetime fields below are kept.

Per process and lock-guarded like vault_probe: the lock is held across the HTTP call, so a burst
of readers that all got a 403 shares one lookup.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Literal, Optional, Tuple

import requests

from app.core.config import get_settings

logger = logging.getLogger(__name__)

TokenState = Literal["ok", "refused", "unknown", "unsupported", "not_configured"]

_TIMEOUT = (2.0, 3.0)
# How long one answer is reused, per state. A refused token is asked again sooner, so a renewed
# or replaced token shows quickly; an unanswered lookup sooner still.
_REUSE_SECONDS = {"ok": 60.0, "refused": 10.0, "unknown": 5.0, "unsupported": 300.0, "not_configured": 60.0}
# A reader that just got a 403 asks again once the cached "ok" is older than this, so a token that
# lapsed a moment ago is not read as a policy refusal for a whole minute. It also bounds how often
# a caller who can provoke 403s (a policy narrower than the path it asks for) makes us ask.
REPROBE_ON_403_SECONDS = 5.0


@dataclass(frozen=True)
class TokenStatus:
    """The token's state and lifetime. Never the token, its id or its accessor."""
    state: TokenState
    checked_at: float
    ttl_seconds: Optional[int] = None
    renewable: Optional[bool] = None
    period_seconds: Optional[int] = None
    expire_time: Optional[str] = None
    is_root: bool = False
    policies: Tuple[str, ...] = ()


Listener = Callable[[Optional[TokenStatus], TokenStatus], None]

_lock = threading.Lock()
_last: Optional[TokenStatus] = None
_listeners: List[Listener] = []


def _close(response) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _int(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _token_still_lists(addr: str, token: str, registry_path: str) -> Optional[bool]:
    """Whether the token can still LIST the cluster registry: an answer other than 403 means Vault
    accepted it. True for 200 or 404, False for 403, None when Vault did not answer."""
    path = registry_path.replace("/data/", "/metadata/", 1).strip("/")
    try:
        response = requests.request(
            "LIST", f"{addr}/v1/{path}", headers={"X-Vault-Token": token},
            timeout=_TIMEOUT, allow_redirects=False,
        )
    except requests.RequestException:
        return None
    try:
        if response.status_code == 403:
            return False
        return response.status_code in (200, 404)
    finally:
        _close(response)


def _ask(addr: str, token: str, registry_path: str) -> TokenStatus:
    now = time.time()
    try:
        response = requests.get(
            f"{addr}/v1/auth/token/lookup-self", headers={"X-Vault-Token": token},
            timeout=_TIMEOUT, allow_redirects=False,
        )
    except requests.RequestException:
        return TokenStatus("unknown", now)
    try:
        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                return TokenStatus("unknown", now)
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, dict):
                return TokenStatus("unknown", now)
            policies = tuple(p for p in (data.get("policies") or []) if isinstance(p, str))
            expire_time = data.get("expire_time")
            return TokenStatus(
                "ok", now,
                ttl_seconds=_int(data.get("ttl")),
                renewable=bool(data.get("renewable")),
                period_seconds=_int(data.get("period")),
                expire_time=expire_time if isinstance(expire_time, str) else None,
                is_root="root" in policies,
                policies=policies,
            )
        if response.status_code == 403:
            # A token without Vault's `default` policy cannot look itself up yet works. Telling it
            # from a dead token takes one read it must be able to do: the registry LIST.
            lists = _token_still_lists(addr, token, registry_path)
            if lists is True:
                return TokenStatus("unsupported", now)
            return TokenStatus("refused" if lists is False else "unknown", now)
        return TokenStatus("unknown", now)
    finally:
        _close(response)


def _announce(before: Optional[TokenStatus], after: TokenStatus) -> None:
    if after.state == "ok":
        logger.info("Vault token probe: ok")
    elif after.state == "refused":
        logger.error("Vault token probe: refused (Vault rejects the dashboard's token; it expired or was revoked)")
    elif after.state == "unsupported":
        logger.error(
            "Vault token probe: unsupported (the token works but cannot look itself up; give it "
            "Vault's default policy so expiry can be watched and renewed)"
        )
    elif after.state == "unknown":
        logger.warning("Vault token probe: unknown (Vault did not answer the token lookup)")
    for listener in list(_listeners):
        try:
            listener(before, after)
        except Exception:  # noqa: BLE001 - a listener must never break a Vault read
            logger.warning("A Vault token state listener failed", exc_info=True)


def status(*, max_age: Optional[float] = None, settings=None) -> TokenStatus:
    """The token's state, asked at most once per reuse window (shorter with `max_age`). Blocking.

    `settings` defaults to the process settings; Host Health passes its own check context's.
    """
    global _last
    settings = settings or get_settings()
    if not settings.vault_enabled or not settings.vault_addr or not settings.vault_token:
        return TokenStatus("not_configured", time.time())
    with _lock:
        if _last is not None:
            window = _REUSE_SECONDS[_last.state]
            if max_age is not None:
                window = min(window, max_age)
            if time.time() - _last.checked_at < window:
                return _last
        registry = getattr(settings, "vault_clusters_path", None) or "secret/data/clusters"
        fresh = _ask(settings.vault_addr.rstrip("/"), settings.vault_token, registry)
        before = _last
        _last = fresh
    if before is None or before.state != fresh.state:
        _announce(before, fresh)
    return fresh


def refusal_explains(status_code: Optional[int]) -> bool:
    """For a Vault reader that got `status_code`: whether the dashboard's token being refused, or
    Vault not answering for it, explains the failure. Only a 403 can be explained this way.

    "unknown" counts: a 403 while Vault cannot say whether the token is alive is an outage, never a
    credential the caller got wrong, and the caller cannot bring that state about.
    """
    if status_code != 403:
        return False
    return status(max_age=REPROBE_ON_403_SECONDS).state in ("refused", "unknown")


def _vault_failure_in(exc: BaseException) -> Optional[BaseException]:
    from app.core.vault_client import VaultClientError

    seen = 0
    current: Optional[BaseException] = exc
    while current is not None and seen < 6:
        if isinstance(current, (VaultClientError, requests.RequestException)):
            return current
        current = current.__cause__ or current.__context__
        seen += 1
    return None


def store_failed(exc: BaseException) -> bool:
    """True when Vault failed as a store (sealed, down, unreachable, busy, or refusing the
    dashboard's own token), never for a document that is not there or a path the policy denies.

    The first Vault or transport error in the exception chain decides, so a wrapper such as
    LXDCertManagerError is classified by the Vault failure behind it.
    """
    from app.core.vault_client import VaultClientError

    failure = _vault_failure_in(exc)
    if failure is None:
        return False
    status_code = getattr(failure, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(failure, "response", None), "status_code", None)
    if status_code is None and isinstance(failure, VaultClientError):
        # important: no status and no transport error behind it is the client refusing the path
        # before any request (an empty or dot segment in a caller's key id). Counted as an
        # outage, anyone could fake the 503 with `X-API-Key-ID: ..`.
        return isinstance(failure.__cause__ or failure.__context__, requests.RequestException)
    if status_code is None or status_code >= 500 or status_code == 429:
        return True
    return refusal_explains(status_code)


def peek() -> Optional[TokenStatus]:
    """The last answer, without asking. Never takes the lock (see vault_probe.peek)."""
    return _last


def remember(fresh: TokenStatus) -> None:
    """Keep an answer obtained elsewhere (the renewal job's own lookup) for later readers."""
    global _last
    with _lock:
        before = _last
        _last = fresh
    if before is None or before.state != fresh.state:
        _announce(before, fresh)


def invalidate() -> None:
    """Make the next status() ask Vault, keeping the last answer for change detection."""
    global _last
    with _lock:
        if _last is not None:
            _last = TokenStatus(_last.state, 0.0, _last.ttl_seconds, _last.renewable, _last.period_seconds,
                                _last.expire_time, _last.is_root, _last.policies)


def on_change(listener: Listener) -> None:
    """Call `listener(before, after)` once per change of state in this process."""
    if listener not in _listeners:
        _listeners.append(listener)


def reset(*, listeners: bool = True) -> None:
    """Test hook. Not called by application code."""
    global _last
    with _lock:
        _last = None
    if listeners:
        _listeners.clear()
