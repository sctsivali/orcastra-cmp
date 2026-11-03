"""Identity provider: whether sign-in can work, and whether role sync can reach the admin API.

Down when the issuer's discovery document does not answer from the backend, or when signed-in
requests are being verified with keys the backend could not refresh. Degraded when only the admin
API (role and group sync) fails. The browser reaches the IdP on its own path, which the backend
cannot see; a sign-in can still fail while this reads healthy, never the reverse.

important: never `authentik_sync.get_sync_status()` here. It runs three group searches per call
(it once also created missing role groups), and this runs every 30 seconds; the token probe below
answers the same question with one cached read.
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import requests

from app.services.host_health.context import CheckContext
from app.services.host_health.format import duration, host_of
from app.services.host_health.models import CheckResult, HealthFact, fact

LABEL = "Identity Provider"
_TIMEOUT = (2.0, 3.0)
ADMIN_API_EVERY = 120.0

_admin_lock = threading.Lock()
_admin_cache: Optional[Tuple[float, str]] = None


def _discovery(issuer: str) -> Optional[str]:
    """None when discovery answers with a JWKS URI, else a short reason without the address."""
    try:
        response = requests.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration",
                                timeout=_TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        return f"{type(exc).__name__}: no answer"
    if response.status_code != 200:
        return f"discovery answered HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return "discovery answered a body that is not JSON"
    return None if isinstance(body, dict) and body.get("jwks_uri") else "discovery has no jwks_uri"


def _admin_api(api_url: str, token: str, now: float) -> str:
    """"ok", "not ready", "token refused" or "unreachable"; cached per process for ADMIN_API_EVERY."""
    global _admin_cache
    with _admin_lock:
        if _admin_cache is not None and now - _admin_cache[0] < ADMIN_API_EVERY:
            return _admin_cache[1]
    from app.services.authentik_sync import api_base

    parsed = urlparse(api_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    # important: the same root role sync uses. Dev sets a bare domain, and asking
    # {domain}/core/users/me/ answered 404, which read as "admin API not ready".
    api_root = api_base(api_url)
    verdict = "ok"
    try:
        ready = requests.get(f"{origin}/-/health/ready/", timeout=_TIMEOUT, allow_redirects=False)
        if not 200 <= ready.status_code < 300:
            verdict = "not ready"
        else:
            # Read-only and cheap: who the token belongs to. Only the status code is kept.
            me = requests.get(f"{api_root}/core/users/me/", timeout=_TIMEOUT, allow_redirects=False,
                              headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            if me.status_code in (401, 403):
                verdict = "token refused"
            elif me.status_code != 200:
                verdict = "not ready"
    except requests.RequestException:
        verdict = "unreachable"
    with _admin_lock:
        _admin_cache = (now, verdict)
    return verdict


def check(ctx: CheckContext) -> CheckResult:
    settings = ctx.settings
    started = time.monotonic()
    if not (settings.auth_enabled and settings.authentik_issuer):
        return CheckResult(id="identity", label=LABEL, status="not_configured", checked_at=ctx.now,
                           summary="Sign-in is not enabled (AUTH_ENABLED or AUTHENTIK_ISSUER is unset).")
    from app.core.auth import _jwks_client

    issuer = settings.authentik_issuer
    facts: List[HealthFact] = [fact("Issuer", host_of(issuer))]
    status, severity = "ok", "info"
    summary = "The identity provider answers and signing keys are current."
    remediation = None

    problem = _discovery(issuer)
    facts.append(fact("Sign-In Discovery", "answering" if problem is None else problem))
    keys = _jwks_client.snapshot(settings)
    if keys.get("keys_held"):
        facts.append(fact("Signing Keys", f"{keys.get('key_count')} held, refreshed {duration(keys.get('age_seconds'))} ago"))
    else:
        facts.append(fact("Signing Keys", "none held yet"))

    if problem is not None:
        status, severity = "down", "critical"
        summary = "The identity provider is not answering. New sign-ins and token refreshes will fail."
        remediation = "Check that the identity provider (AUTHENTIK_ISSUER) is running and reachable from the dashboard host."
    elif keys.get("serving_stale"):
        status, severity = "degraded", "warning"
        summary = ("Signing keys could not be refreshed; requests are verified with the last good keys "
                   f"for up to {duration(keys.get('max_stale_seconds'))}.")
        remediation = "Check the identity provider's JWKS endpoint from the dashboard host."

    if settings.authentik_api_url and settings.authentik_api_token:
        verdict = _admin_api(settings.authentik_api_url, settings.authentik_api_token, ctx.now)
        facts.append(fact("Admin API (Role Sync)", verdict))
        if verdict != "ok" and status == "ok":
            status, severity = "degraded", "warning"
            summary = {
                "token refused": "The identity provider refuses the dashboard's admin API token. Role and group sync will fail.",
            }.get(verdict, "The identity provider's admin API is not ready. Role and group sync will fail.")
            remediation = ("Issue a new API token in the identity provider and update AUTHENTIK_API_TOKEN."
                           if verdict == "token refused" else
                           "Check AUTHENTIK_API_URL and that the identity provider's workers are running.")
    else:
        facts.append(fact("Admin API (Role Sync)", "not configured"))

    return CheckResult(id="identity", label=LABEL, status=status, severity=severity, summary=summary, facts=facts,
                       remediation=remediation, checked_at=ctx.now,
                       latency_ms=int((time.monotonic() - started) * 1000))


def reset() -> None:
    """Test hook."""
    global _admin_cache
    with _admin_lock:
        _admin_cache = None
