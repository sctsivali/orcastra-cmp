"""Operational events: the dashboard telling the people who run it about a fault, in their own tools.

One audit record per transition (firing, then resolved), written through the audit logger, so it
lands where operators already look: the Cluster Log in the console (Administrators see every event,
partners see their clusters' events, tenants none: core/task_scope.py) and the deployment's own
OpenSearch, in `orcastra-audit-*` (filter `action: system.*` or `category: operations`). Every
record carries a plain summary, the fix, and a link to the public guide.

A transition is reported once across workers. The current state of each event is swapped
atomically in Redis (SET ... GET), so only the worker that changed it reports. The event id is
derived from the transition and a ten-minute bucket, so with Redis down the journal (unique
event_id) and OpenSearch (event_id is the document id) still collapse two workers' reports of
the same change.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Literal, Mapping, Optional

logger = logging.getLogger(__name__)

Scope = Literal["deployment", "cluster"]
Severity = Literal["critical", "warning", "info"]

# One page in the public guide lists every event with its fix; each event links its own heading.
DOCS_PAGE = "https://docs.orcastra.io/en/latest/operations/operational-events/"


@dataclass(frozen=True)
class OpsEvent:
    code: str
    scope: Scope
    severity: Severity
    summary: str
    fix: str
    # The summary once the fault clears; None for an event that is history only (it never "fires").
    resolved: Optional[str]
    docs_url: str


CATALOGUE: Dict[str, OpsEvent] = {
    event.code: event
    for event in (
        OpsEvent(
            "vault.token_refused", "deployment", "critical",
            "Vault rejects the dashboard's token (it expired or was revoked). Cluster access, "
            "certificates and API keys are unavailable.",
            "Create a periodic token on the Vault host (vault token create -orphan "
            "-policy=orcastra-policy -period=720h), put it in VAULT_TOKEN and recreate the backend.",
            "Vault accepts the dashboard's token again.",
            DOCS_PAGE + "#vault-token-refused",
        ),
        OpsEvent(
            "vault.token_expiring", "deployment", "warning",
            "The dashboard's Vault token expires on {expire_time} and cannot be renewed past it.",
            "Replace it with a periodic token (-period=720h) before then; the dashboard renews a "
            "periodic token itself.",
            "The dashboard's Vault token no longer expires soon.",
            DOCS_PAGE + "#vault-token-expiring",
        ),
        OpsEvent(
            "vault.token_renewal_failed", "deployment", "warning",
            "The dashboard could not renew its Vault token ({reason}).",
            "Check that Vault is reachable and that the token keeps Vault's default policy.",
            "The dashboard renews its Vault token again.",
            DOCS_PAGE + "#vault-token-renewal-failed",
        ),
        OpsEvent(
            "vault.token_renewed", "deployment", "info",
            "The dashboard renewed its Vault token; it now lasts {lease}.",
            "",
            None,
            DOCS_PAGE + "#vault-token-renewed",
        ),
        OpsEvent(
            "cluster.certificate_expiring", "cluster", "warning",
            "The dashboard's certificate for cluster {cluster} expires on {not_after} (in {days} days).",
            "Renew it from Clusters, Certificates before then; once it lapses the cluster refuses "
            "the dashboard.",
            "The dashboard's certificate for cluster {cluster} is current again (expires {not_after}).",
            DOCS_PAGE + "#cluster-certificate-expiring",
        ),
        OpsEvent(
            "cluster.certificate_expired", "cluster", "critical",
            "The dashboard's certificate for cluster {cluster} expired on {not_after}; the cluster "
            "refuses the dashboard.",
            "Regenerate the certificate from Clusters, Certificates and add it to the cluster's "
            "trust store.",
            "The dashboard's certificate for cluster {cluster} is current again (expires {not_after}).",
            DOCS_PAGE + "#cluster-certificate-expired",
        ),
    )
}

ACTION_PREFIX = "system."
_STATE_TTL_SECONDS = 30 * 24 * 3600
_BUCKET_SECONDS = 600
_EVENT_NAMESPACE = uuid.UUID("6f0b8f3e-9f5a-4a51-9a37-3c0d6f7a2b11")
_AUDIT_SEVERITY = {"critical": "critical", "warning": "high", "info": "low"}

_local_lock = threading.Lock()
# Per-process fallback for the shared state while Redis is unreachable.
_local_state: Dict[str, str] = {}


def action_for(code: str) -> str:
    return f"{ACTION_PREFIX}{code}"


def _state_key(code: str, cluster_id: Optional[str]) -> str:
    return f"orcastra:ops_event:{code}:{cluster_id or '-'}"


def _redis() -> Any:
    try:
        from app.core.cache import get_cache_manager

        return getattr(get_cache_manager(), "redis_client", None)
    except Exception:  # noqa: BLE001 - no cache manager is "no Redis"
        return None


def _swap(key: str, value: str) -> Optional[str]:
    """Store `value` and return what was there: atomic across workers when Redis answers."""
    client = _redis()
    if client is not None:
        try:
            previous = client.set(key, value, ex=_STATE_TTL_SECONDS, get=True)
            if isinstance(previous, bytes):
                previous = previous.decode()
            with _local_lock:
                _local_state[key] = value
            return previous
        except Exception:  # noqa: BLE001 - fall back to this process's own memory
            logger.debug("ops event state could not be swapped in Redis", exc_info=True)
    with _local_lock:
        previous = _local_state.get(key)
        _local_state[key] = value
    return previous


class _Facts(dict):
    def __missing__(self, key: str) -> str:
        return "unknown"


def _render(template: str, facts: Mapping[str, Any]) -> str:
    try:
        return template.format_map(_Facts({k: v for k, v in facts.items() if v is not None}))
    except (ValueError, IndexError):
        return template


def _event_id(code: str, cluster_id: Optional[str], state: str, now: float) -> str:
    bucket = int(now // _BUCKET_SECONDS)
    return str(uuid.uuid5(_EVENT_NAMESPACE, f"{code}|{cluster_id or '-'}|{state}|{bucket}"))


def _emit(event: OpsEvent, state: str, severity: str, cluster_id: Optional[str], facts: Mapping[str, Any]) -> None:
    from app.services.activity_logger import get_activity_logger

    firing = state in ("firing", "recorded")
    summary = _render(event.resolved if state == "resolved" and event.resolved else event.summary, facts)
    details: Dict[str, Any] = {
        "event_code": event.code,
        "state": state,
        "severity": severity,
        "summary": summary,
        "docs_url": event.docs_url,
    }
    if firing and event.fix:
        details["fix"] = event.fix
    details.update({k: v for k, v in facts.items() if v is not None})
    now = time.time()
    try:
        # important: written outside the caller's request context. The request that happened to
        # notice the change (any role's status poll) is not its actor, and a row with no cluster is
        # shown to its own actor (core/task_scope.py), so a tenant would see a deployment event.
        contextvars.Context().run(
            get_activity_logger().log_activity,
            action=action_for(event.code),
            target_type="cluster" if cluster_id else "deployment",
            target_id=cluster_id or "dashboard",
            target_host=cluster_id,
            result="success",
            details=details,
            severity=_AUDIT_SEVERITY[severity] if firing else "low",
            event_id=_event_id(event.code, cluster_id, f"{state}:{severity}", now),
        )
    except Exception:  # noqa: BLE001 - reporting a fault must never become one
        logger.warning("Could not record operational event %s", event.code, exc_info=True)
        return
    level = (logging.ERROR if severity == "critical" else logging.WARNING if severity == "warning"
             else logging.INFO) if state == "firing" else logging.INFO
    logger.log(level, "Operational event %s %s: %s", event.code, state, summary,
               extra={"extra_fields": {"event_code": event.code, "event_state": state, "cluster_id": cluster_id}})


def _event(code: str, cluster_id: Optional[str]) -> OpsEvent:
    event = CATALOGUE[code]
    if (event.scope == "cluster") != bool(cluster_id):
        raise ValueError(f"{code} is a {event.scope} event")
    return event


def fire(code: str, *, cluster_id: Optional[str] = None, severity: Optional[Severity] = None,
         facts: Optional[Mapping[str, Any]] = None) -> bool:
    """Report that a fault holds. Reported once per change of state or severity; True if reported."""
    event = _event(code, cluster_id)
    level = severity or event.severity
    if _swap(_state_key(code, cluster_id), f"firing:{level}") == f"firing:{level}":
        return False
    _emit(event, "firing", level, cluster_id, facts or {})
    return True


def resolve(code: str, *, cluster_id: Optional[str] = None, facts: Optional[Mapping[str, Any]] = None) -> bool:
    """Report that a fault cleared, if it was firing. True if reported."""
    event = _event(code, cluster_id)
    previous = _swap(_state_key(code, cluster_id), "resolved")
    if previous is None or not previous.startswith("firing"):
        return False
    _emit(event, "resolved", "info", cluster_id, facts or {})
    return True


def clear(code: str, *, cluster_id: Optional[str] = None) -> None:
    """Forget a firing state without reporting it, when a successor event takes over (an expiring
    certificate that has now expired): the successor's own resolution is the one reported."""
    _event(code, cluster_id)
    _swap(_state_key(code, cluster_id), "resolved")


def record(code: str, *, cluster_id: Optional[str] = None, facts: Optional[Mapping[str, Any]] = None) -> None:
    """Report a history-only event (it has no resolved state), every time."""
    event = _event(code, cluster_id)
    _emit(event, "recorded", event.severity, cluster_id, facts or {})


def reset() -> None:
    """Test hook. Not called by application code."""
    with _local_lock:
        _local_state.clear()
