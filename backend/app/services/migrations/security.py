"""Security primitives shared by migration preflight and execution."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from app.core.type_utils import detect_cluster_type_from_metadata

_SECRET_MARKERS = (
    "authorization",
    "certificate",
    "cloud-init",
    "cloud_init",
    "cookie",
    "credential",
    "private",
    "password",
    "secret",
    "token",
    "user-data",
    "user_data",
)
_OPERATION_PATH = re.compile(r"^/1\.0/operations/([A-Za-z0-9._:-]+)$")
_SECRET_LABEL = (
    r"password|token|secret|authorization|cookie|certificate|credential|"
    r"private[_-]?key|client[_-]?secret"
)
_BARE_TOKEN_PATTERNS = (
    # Compact JWT/JWS values. Require three substantial base64url segments to avoid redacting
    # ordinary dotted identifiers and version strings.
    re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])"),
    # Common provider token families that are routinely pasted into operator reason fields.
    re.compile(
        r"(?<![A-Za-z0-9_-])(?:github_pat_|gh[pousr]_|glpat-|sk-|xox[baprs]-)[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])",
        flags=re.IGNORECASE,
    ),
    re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
)


class MigrationSecurityError(ValueError):
    """A daemon-provided value violated a migration trust boundary."""


def canonical_json_hash(value: Any) -> str:
    """Stable request hash used by plan revisions and idempotency checks."""
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_resource_key(
    scope: str,
    cluster_id: str,
    project: str,
    resource_name: Optional[str],
) -> str:
    """A portable lock identity. Values are hashed to keep index keys bounded."""
    raw = "\x00".join(
        (scope, cluster_id.strip(), (project or "default").strip(), resource_name or "*")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def instance_identity_fingerprint(instance: Mapping[str, Any]) -> Optional[str]:
    """Hash immutable, non-secret daemon evidence for one instance identity.

    Names are reusable, so neither a canonical resource key nor a definition hash proves that
    the daemon object reviewed during preflight is still the object being migrated. Persist only
    this digest, never the raw volatile UUID values.
    """
    config = instance.get("config") or {}
    if not isinstance(config, Mapping):
        config = {}
    evidence = {
        "created_at": instance.get("created_at"),
        "volatile.uuid": config.get("volatile.uuid"),
        "volatile.uuid.generation": config.get("volatile.uuid.generation"),
    }
    evidence = {key: value for key, value in evidence.items() if value not in (None, "")}
    return canonical_json_hash(evidence) if evidence else None


def sanitize_payload(value: Any) -> Any:
    """Recursively redact secret-bearing values before persistence or API output."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, nested in value.items():
            label = str(key)
            if any(marker in label.lower() for marker in _SECRET_MARKERS):
                result[label] = "[redacted]"
            else:
                result[label] = sanitize_payload(nested)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize_payload(nested) for nested in value]
    if isinstance(value, str):
        return sanitize_error(value, limit=2000)
    return value


def sanitize_error(value: Any, *, limit: int = 1000) -> str:
    """Bound an operator-facing error and remove common inline secret shapes."""
    text = str(value or "Migration failed")
    text = re.sub(
        r"-----BEGIN [^-\r\n]{1,64}-----.*?-----END [^-\r\n]{1,64}-----",
        "[redacted-pem]",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [redacted]",
        text,
    )
    text = re.sub(
        rf'''(?i)(["'](?:{_SECRET_LABEL})["']\s*:\s*)(["'])[^"']*\2''',
        r'\1"[redacted]"',
        text,
    )
    text = re.sub(
        rf"(?i)({_SECRET_LABEL})\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(
        rf"(?i)(/(?:{_SECRET_LABEL})/)[^/?#\s]+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)(https?://[^:/@\s]+:)[^@/\s]+@",
        r"\1[redacted]@",
        text,
    )
    for pattern in _BARE_TOKEN_PATTERNS:
        text = pattern.sub("[redacted-token]", text)
    return text[:limit]


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise MigrationSecurityError("Operation URL must not contain user information.")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MigrationSecurityError("Operation URL is not an absolute HTTP(S) URL.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.lower(), port


def validate_operation_url(operation_url: str, api_endpoint: str) -> tuple[str, str]:
    """Bind a relative or absolute operation URL to the registered API origin."""
    base = api_endpoint.rstrip("/") + "/"
    absolute = urljoin(base, operation_url)
    parsed = urlparse(absolute)
    if parsed.query or parsed.fragment:
        raise MigrationSecurityError("Operation URL must not contain a query or fragment.")
    if _origin(absolute) != _origin(base):
        raise MigrationSecurityError("Operation URL does not belong to the registered cluster.")
    match = _OPERATION_PATH.fullmatch(parsed.path.rstrip("/"))
    if not match:
        raise MigrationSecurityError("Daemon returned an invalid operation path.")
    return absolute, match.group(1)


def daemon_identity(client: Any) -> dict[str, Any]:
    """Return a public capability projection from trusted cached `/1.0` metadata."""
    info = getattr(client, "host_info", None)
    if not isinstance(info, dict) or info.get("auth") != "trusted":
        raise MigrationSecurityError("The cluster did not provide trusted server metadata.")
    environment = info.get("environment") or {}
    family = detect_cluster_type_from_metadata(info)
    extensions = sorted(
        extension
        for extension in (info.get("api_extensions") or [])
        if isinstance(extension, str)
    )
    return {
        "engine": "incus" if family.lower() == "incus" else "lxd",
        "family": family,
        "version": str(environment.get("server_version") or ""),
        "clustered": bool(environment.get("server_clustered", False)),
        "architectures": [
            value
            for value in (environment.get("architectures") or [])
            if isinstance(value, str)
        ],
        "extensions": extensions,
    }


def _exact_build_allowlist(configured: Any) -> set[str]:
    if isinstance(configured, str):
        return {value.strip().lower() for value in configured.split(",") if value.strip()}
    return {str(value).strip().lower() for value in configured if str(value).strip()}


def _build_token(identity: Mapping[str, Any]) -> str:
    return f"{identity.get('engine', '')}:{identity.get('version', '')}".lower()


def approved_build(identity: Mapping[str, Any], settings: Any) -> bool:
    """Require the explicit operator allowlist without a security-bypass switch."""
    allowlist = _exact_build_allowlist(
        getattr(settings, "migration_approved_builds", "") or ""
    )
    return _build_token(identity) in allowlist


def approved_lxd_native_cross_project_move(
    identity: Mapping[str, Any], settings: Any
) -> bool:
    """Require an independent exact-build approval for the GHSA-affected native path.

    Distribution backports make server-version ordering insufficient evidence. This gate has no
    disable switch: an operator must explicitly attest the exact build after vendor or package
    review even when the broader migration-build gate is disabled.
    """
    if str(identity.get("engine") or "").lower() != "lxd":
        return False
    if not str(identity.get("version") or "").strip():
        return False
    allowlist = _exact_build_allowlist(
        getattr(
            settings,
            "migration_lxd_native_cross_project_move_approved_builds",
            "",
        )
        or ""
    )
    return _build_token(identity) in allowlist


def api_endpoint(client: Any) -> str:
    endpoint = getattr(getattr(client, "api", None), "_api_endpoint", None)
    if not isinstance(endpoint, str) or not endpoint:
        raise MigrationSecurityError("The registered cluster endpoint is unavailable.")
    return endpoint
