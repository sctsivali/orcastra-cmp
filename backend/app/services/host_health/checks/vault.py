"""Vault: sealed, uninitialized, unreachable, refusing the registry, or a token about to lapse.

The state is the one the Vault notice already shows (services/vault_health.py), so the banner and
this page never disagree. This adds what an operator needs next: the seal's threshold and progress,
whether the address is one node of an HA cluster, and when the dashboard's own token expires.
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional, Tuple

import requests

from app.core import vault_probe
from app.core.vault_probe import SealFacts
from app.services import vault_unseal
from app.services.host_health.context import CheckContext
from app.services.host_health.format import duration, host_of
from app.services.host_health.models import CheckResult, HealthFact, fact
from app.services.vault_health import resolve_vault_health

LABEL = "Vault"
_TIMEOUT = (2.0, 3.0)
TOKEN_LOOKUP_EVERY = 300.0
TOKEN_WARN_SECONDS = 7 * 24 * 3600

_token_lock = threading.Lock()
# (looked up at, facts) per process: the token's TTL moves slowly, Vault need not be asked each pass.
_token_cache: Optional[Tuple[float, dict]] = None

_SUMMARIES = {
    "sealed": "Vault is sealed. Cluster access and certificates are unavailable until it is unsealed.",
    "uninitialized": "Vault is not initialized. The dashboard cannot read or store any cluster credentials.",
    "unreachable": "The dashboard cannot reach Vault. Cluster access and certificates are unavailable.",
    "unavailable": "Vault is unsealed but refuses the dashboard's cluster registry read.",
}
_REMEDIATION = {
    "sealed": "Unseal Vault: enter the threshold number of key shares here, or run `vault operator unseal` on the Vault host.",
    "uninitialized": "Initialize Vault with the installer (it creates the unseal keys), then restore the dashboard's token.",
    "unreachable": "Check that Vault is running and that VAULT_ADDR is reachable from the backend container.",
    "unavailable": "Check that VAULT_TOKEN is valid and its policy allows the cluster registry path (VAULT_CLUSTERS_PATH).",
}


def _token_facts(addr: str, token: str, now: float) -> dict:
    """{"ttl", "renewable", "root", "readable"}; never the token, never Vault's error text."""
    global _token_cache
    with _token_lock:
        if _token_cache is not None and now - _token_cache[0] < TOKEN_LOOKUP_EVERY:
            return _token_cache[1]
    facts: dict = {"readable": False}
    try:
        response = requests.get(
            f"{addr.rstrip('/')}/v1/auth/token/lookup-self",
            headers={"X-Vault-Token": token}, timeout=_TIMEOUT, allow_redirects=False,
        )
        if response.status_code == 200:
            data = (response.json() or {}).get("data") or {}
            policies = data.get("policies") or []
            facts = {
                "readable": True,
                "ttl": data.get("ttl") if isinstance(data.get("ttl"), int) else None,
                "renewable": bool(data.get("renewable")),
                "root": "root" in policies,
            }
    except (requests.RequestException, ValueError, AttributeError):
        facts = {"readable": False}
    with _token_lock:
        _token_cache = (now, facts)
    return facts


def _leader(addr: str) -> dict:
    try:
        response = requests.get(f"{addr.rstrip('/')}/v1/sys/leader", timeout=_TIMEOUT, allow_redirects=False)
        body = response.json() if response.status_code == 200 else {}
        return body if isinstance(body, dict) else {}
    except (requests.RequestException, ValueError):
        return {}


def _seal_facts(facts: SealFacts, state: str) -> List[HealthFact]:
    out: List[HealthFact] = []
    if facts.seal_type:
        out.append(fact("Seal Type", facts.seal_type))
    if facts.threshold and facts.shares:
        out.append(fact("Key Threshold", f"{facts.threshold} of {facts.shares} key shares"))
    if state == "sealed" and facts.threshold:
        out.append(fact("Unseal Progress", f"{facts.progress or 0} of {facts.threshold} keys entered"))
    if facts.version:
        out.append(fact("Version", facts.version))
    if facts.storage_type:
        out.append(fact("Storage", facts.storage_type))
    return out


def check(ctx: CheckContext) -> CheckResult:
    settings = ctx.settings
    started = time.monotonic()
    if not settings.vault_enabled or not settings.vault_addr:
        return CheckResult(id="vault", label=LABEL, status="not_configured", checked_at=ctx.now,
                           summary="Vault integration is not enabled (VAULT_ENABLED).")
    from app.services.lxd.lxd_service import get_lxd_service

    health = resolve_vault_health(get_lxd_service().cert_manager)
    probe = vault_probe.peek()
    seal = (probe.facts if probe else None) or SealFacts()
    addr = settings.vault_addr
    facts: List[HealthFact] = [fact("Address", host_of(addr))]
    facts += _seal_facts(seal, health.state)
    advisories: List[str] = []

    leader = _leader(addr) if health.state in ("ok", "sealed") else {}
    if leader.get("ha_enabled"):
        facts.append(fact("High Availability", "active node" if leader.get("is_self") else "standby node"))
        if health.state == "sealed":
            advisories.append(
                "Vault runs with high availability: every node unseals separately. "
                f"This page unseals only the node at {host_of(addr)}."
            )
    if health.state in ("ok", "unavailable"):
        source = {"vault": "readable", "last_known": "served from the last known copy"}.get(health.registry_source or "")
        if source:
            facts.append(fact("Cluster Registry", source))
    if vault_unseal.insecure_transport(addr):
        advisories.append(
            "The backend reaches Vault over plain HTTP. Unseal keys entered here cross that link "
            "unencrypted; keep it on a private network or enable TLS on Vault."
        )

    status, severity = "ok", "info"
    summary = "Vault is unsealed and the cluster registry is readable."
    remediation = None
    if health.state in _SUMMARIES:
        status, severity = "down", "critical"
        summary = _SUMMARIES[health.state]
        remediation = _REMEDIATION[health.state]

    if health.state == "ok" and settings.vault_token:
        token = _token_facts(addr, settings.vault_token, ctx.now)
        if token.get("readable"):
            ttl = token.get("ttl")
            facts.append(fact("Token Expiry", "never" if not ttl else f"in {duration(ttl)}"))
            if token.get("root"):
                advisories.append(
                    "The dashboard uses a root token. A token limited to the dashboard's policy limits "
                    "what a leaked backend environment could do."
                )
            if ttl and ttl < TOKEN_WARN_SECONDS:
                status, severity = "degraded", "warning"
                summary = f"The dashboard's Vault token expires in {duration(ttl)}."
                remediation = (
                    "Renew the token (`vault token renew`) or issue a new one and update VAULT_TOKEN "
                    "before it lapses; when it does, every cluster becomes unreachable."
                )
        else:
            facts.append(fact("Token Expiry", "not readable with this token's policy"))

    actions = []
    allowed, reason = vault_unseal.availability(settings, probe)
    if health.state == "sealed":
        if allowed:
            actions.append("unseal_vault")
        elif reason:
            facts.append(fact("Unseal from the Dashboard", reason))
    return CheckResult(
        id="vault", label=LABEL, status=status, severity=severity, summary=summary, facts=facts,
        advisories=advisories, remediation=remediation, actions=actions, checked_at=ctx.now,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


def reset() -> None:
    """Test hook."""
    global _token_cache
    with _token_lock:
        _token_cache = None
