"""One place that decides what an error message may tell a client.

`lib/api.ts` surfaces the backend's `detail` verbatim as `error.message`, and many handlers answer
with `detail=str(e)`. That exception text comes from pylxd, requests, asyncpg and hvac, and it can
carry a cluster's control-plane URL and port, a TLS failure naming the certificate, a filesystem or
Vault path, or a SQL fragment. Most of those routes serve tenants. This is the single choke point,
applied to server faults only.

A 4xx is usually the caller's own input described back to them, the message an integrator needs to
fix the request, so it is left alone. A 5xx is the server failing; the caller gets a stable
reference that ties their report to the full text in the server log.
"""

import re
import uuid
from typing import Any, Dict, Tuple

# URLs, and any path-looking run of two or more segments. The text has no known shape, so this
# prefers removing something harmless over keeping an endpoint.
_SENSITIVE = re.compile(r"(https?://\S+|(?:/[\w.\-]+){2,})")
_SECRET_LABEL = (
    r"password|token|secret|authorization|cookie|certificate|credential|"
    r"private[_-]?key|client[_-]?secret"
)
_BARE_TOKEN_PATTERNS = (
    re.compile(
        r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{12,}\."
        r"[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])"
    ),
    re.compile(
        r"(?<![A-Za-z0-9_-])(?:github_pat_|gh[pousr]_|glpat-|sk-|xox[baprs]-)"
        r"[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])",
        flags=re.IGNORECASE,
    ),
    re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
)

# "ticket" covers the single-use WebSocket tickets. They are worthless once redeemed, but a line
# written before the socket connects still holds a live one.
SENSITIVE_PARAM_STEMS = (
    "token", "password", "secret", "api_key", "apikey", "api-key",
    "authorization", "auth", "credential", "signature", "code", "key", "ticket",
)
_QUERY_PAIR = re.compile(r"([?&;])([^=&#\s\"']+)=([^&#;\s\"']*)")

GENERIC_SERVER_ERROR = "Internal server error"


def sanitize_error(message: object, limit: int = 300) -> str:
    """Redact URLs and paths from a message that is still worth showing.

    Used where a specific message helps and the text is only partly ours, ex; a cluster driver
    error surfaced on a 4xx. `main.py` uses `reference_for` instead for server faults.
    """
    try:
        text = "" if message is None else str(message)
    except Exception:  # noqa: BLE001 - error reporting itself must remain best-effort
        text = "Error details unavailable"
    text = _SENSITIVE.sub("[redacted]", text)
    text = re.sub(
        rf"(?i)({_SECRET_LABEL})\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        text,
    )
    for pattern in _BARE_TOKEN_PATTERNS:
        text = pattern.sub("[redacted-token]", text)
    return text.strip()[:limit]


def reference_for(exc: BaseException) -> Tuple[str, str]:
    """A short reference id and the full text to log against it.

    The id is what the client sees. It is random rather than derived from the message, so it
    cannot be used to confirm whether two different callers hit the same internal failure.
    """
    reference = uuid.uuid4().hex[:8]
    return reference, f"{type(exc).__name__}: {exc}"


def client_detail(reference: str) -> str:
    return f"{GENERIC_SERVER_ERROR}. Reference: {reference}"


# The structured 5xx answers a client acts on, by status, each with the message used when the
# raising handler gave none. Only these keep their shape, `{error, message}` or `{code, message}`
# as the handler keyed it; any other detail is flattened to sanitized text, so a handler cannot
# ship arbitrary fields to a client.
_STRUCTURED_5XX: Dict[int, Dict[str, str]] = {
    502: {
        "snapshot_outcome_unknown": "Check this instance's snapshots before trying again.",
        "snapshot_terminal_failure": "The cluster did not complete the snapshot.",
        "vault_error": "Vault refused the unseal request.",
        # services/instance_files/errors.py: the file explorer branches on these.
        "guest_protocol_error": "The guest answered in a way this service does not accept.",
        "guest_io_error": "The guest could not complete this operation.",
        "read_only_fs": "This filesystem is read only.",
        "cluster_refused": "The cluster refused this service's certificate.",
        # routers/my_keys.py: the record stays so the key can be revoked again.
        "key_not_revoked": "The cluster did not confirm the key was removed, so it was kept.",
        # services/guest_credentials/errors.py: the credentials panel names the step that failed.
        "guest_command_failed": "The guest could not complete this change.",
        "user_create_failed": "The guest could not create this account.",
        "user_sudo_failed": "The guest could not change this account's sudo group.",
        "password_set_failed": "The guest could not set this password.",
        "password_expire_failed": "The guest could not require a password change at next login.",
        "password_lock_failed": "The guest could not lock this password.",
        "key_write_failed": "The guest could not write this account's authorized_keys.",
    },
    503: {
        "authority_unavailable": "Current access could not be verified. Retry shortly.",
        "identity_provider_unavailable": (
            "The identity provider could not be reached to verify this sign-in. Retry shortly."
        ),
        "vault_unavailable": (
            "Cluster data cannot be read because the secret store (Vault) is sealed or unavailable."
        ),
        # routers/integrations.py: API keys live in Vault too, so a sealed Vault is neither an
        # invalid key nor an empty key list.
        "api_key_store_unavailable": (
            "API keys cannot be read or written because the secret store (Vault) is sealed or "
            "unavailable."
        ),
        # Host Health's unseal (routers/host_health.py): the dialog tells "nothing was sent" apart
        # from "sent, not confirmed", so the operator knows whether to check progress first.
        "vault_unreachable": "Vault did not answer. Check the progress before entering the key again.",
        "vault_unconfirmed": "Vault did not confirm the request. Check the progress before entering the key again.",
        "unseal_busy": "Another unseal request is still being processed. Try again in a moment.",
        "agent_unavailable": "The guest agent is not reachable inside this virtual machine.",
        "cluster_unreachable": "The cluster did not answer.",
        "cluster_not_pinned": "This cluster's certificate could not be confirmed, so nothing was sent.",
        "unsupported_server": "This cluster does not offer file access.",
        "feature_disabled": "File access is turned off on this deployment.",
        "guest_disconnected": "The connection to the guest closed. The instance may have stopped.",
        # frontend isProjectDeleteUnknown follows the project on this code instead of failing.
        "project_write_unknown": "This project change may still finish. Refresh before retrying.",
        "CREATE_PREFLIGHT_UNAVAILABLE": "Instance preflight is busy or timed out. Try again shortly.",
        "LXD_UNAVAILABLE": "The target cluster is unavailable. The create was not submitted.",
        "operation_cancel_temporarily_unavailable": "Operation cancellation is temporarily unavailable. Retry shortly.",
        "operation_cancel_outcome_unknown": "The cluster may have accepted the cancellation. Refresh before retrying.",
    },
    504: {
        "guest_timeout": "The guest did not answer in time. It may be busy or still starting.",
        # Sent but unconfirmed: the panel tells this apart from a refusal, so the previous password
        # is not assumed to still work.
        "credential_outcome_unknown": (
            "The change was sent but the guest did not confirm it. Check the account before relying on it."
        ),
    },
    507: {
        "no_space": "The guest does not have enough free space.",
    },
}


def public_server_detail(status_code: int, detail: Any, reference: str) -> Any:
    """What a client is shown for a 5xx, decided in one place for REST and MCP alike.

    A 500 is an unexpected fault whose text is a driver or database exception, so it becomes a
    reference. A 502/503/504/507 is an answer this application wrote ("cluster X recently
    unreachable"), so its message stays with URLs and paths stripped. An allowlisted structured
    code keeps its shape so the client can act on it; flattened to a string it would reach the
    browser as a dict repr the client's mapping cannot match.
    """
    if status_code not in (502, 503, 504, 507):
        return client_detail(reference)
    codes = _STRUCTURED_5XX.get(status_code, {})
    if not isinstance(detail, dict):
        return sanitize_error(str(detail))
    message = detail.get("message")
    for key in ("error", "code"):
        code = detail.get(key)
        if isinstance(code, str) and code in codes:
            return {
                key: code,
                "message": sanitize_error(message if isinstance(message, str) else codes[code]),
            }
    # important: never str() a dict. The operator reads this text, and a repr hides the code from
    # the client that branches on it.
    return sanitize_error(message) if isinstance(message, str) else client_detail(reference)


def is_sensitive_param(name: str) -> bool:
    """Whether a parameter name looks like a credential (structured and Uvicorn access logs)."""
    from urllib.parse import unquote_plus

    lowered = unquote_plus(name or "").lower()
    return any(stem in lowered for stem in SENSITIVE_PARAM_STEMS)


def redact_query_secrets(text: str) -> str:
    """Redact credential values in free-text request and WebSocket handshake lines."""
    if not text or "=" not in text:
        return text

    def replace(match: re.Match[str]) -> str:
        separator, name = match.group(1), match.group(2)
        if is_sensitive_param(name):
            return f"{separator}{name}=[REDACTED]"
        return match.group(0)

    return _QUERY_PAIR.sub(replace, text)
