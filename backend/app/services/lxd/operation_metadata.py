"""What of an LXD operation a caller is shown.

An LXD operation carries the credentials for its own sockets in its metadata. An exec or console
operation holds `fds`, whose values open the operation websocket with no further authentication. A
token operation holds `secret`, and a migration source holds `control`, `fs` and `criu`. Anyone who
reads one can attach to the session or take the data it serves.

Two readings, one per kind of surface:
- `sanitize_operation_metadata` is the allowlist. The dashboard's /operations routes apply it, and
  so do the key surfaces that serialize an operation: the `get_operation` proxy operation,
  GET /integrations/operations/{id} and MCP `orcastra_get_operation`.
- `redact_operation_response` is the raw proxy's filter. The raw proxy passes LXD's own JSON through,
  and its callers read LXD's fields (an exec's `return`, an image's `fingerprint`), so there the
  secret fields are removed and the rest of LXD's answer is left as LXD wrote it.
"""

from collections.abc import Mapping
from typing import Any, Dict

from app.core.error_sanitizer import sanitize_error
from app.core.raw_proxy_path import ProxyPath

__all__ = [
    "OPERATION_SECRET_KEYS",
    "SAFE_OPERATION_METADATA_KEYS",
    "redact_operation_response",
    "sanitize_operation_metadata",
    "strip_operation_secrets",
]

# LXD operation metadata is not a presentation object. Console and exec operations can carry
# per-fd websocket secrets there, while migration operations can carry transport credentials.
# Only scalar progress fields have a browser use and are safe to return.
SAFE_OPERATION_METADATA_KEYS = frozenset(
    {
        "progress",
        "download_progress",
        "upload_progress",
        "fs_progress",
        "create_progress",
        "migration_progress",
    }
)

# important: every one of these is a one-time secret that opens an LXD operation websocket (or, for
# a token, joins a client or a cluster member) with no further authentication. `fds` is what an
# exec or console operation carries, `secret` a token operation, and `control`, `fs` and `criu` a
# migration source. Compared lowercased, so no spelling of one passes.
OPERATION_SECRET_KEYS = frozenset(
    {"fds", "secret", "secrets", "token", "trust_token", "control", "fs", "criu"}
)

_OPERATIONS_ROOT = ("operations",)


def sanitize_operation_metadata(value: Any) -> Dict[str, Any]:
    """The allowlisted scalar progress fields of an operation's metadata, error-sanitized."""
    if not isinstance(value, Mapping):
        return {}
    return {
        key: sanitize_error(item, limit=500) if isinstance(item, str) else item
        for key, item in value.items()
        if key in SAFE_OPERATION_METADATA_KEYS
        and (item is None or isinstance(item, (str, int, float, bool)))
    }


def strip_operation_secrets(metadata: Any) -> Dict[str, Any]:
    """An operation's metadata without the fields that grant access to the operation."""
    if not isinstance(metadata, Mapping):
        return {}
    return {
        key: value
        for key, value in metadata.items()
        if str(key).lower() not in OPERATION_SECRET_KEYS
    }


def _scrub(value: Any) -> Any:
    """Every `metadata` map in an LXD body, at any depth, without the secret fields.

    note: shape-agnostic on purpose. The envelope, a listing bucket and one operation all nest an
    operation's own metadata under `metadata`, so walking the body covers the listing in every
    recursion and all-projects form, one operation and its /wait without trusting LXD's layout.
    """
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    scrubbed: Dict[str, Any] = {}
    for key, item in value.items():
        if key == "metadata" and isinstance(item, Mapping):
            item = strip_operation_secrets(item)
        scrubbed[key] = _scrub(item)
    return scrubbed


def redact_operation_response(path: ProxyPath, data: Any) -> Any:
    """LXD's answer to a raw-proxy request under /1.0/operations, without the operations' secrets.

    Any other path is returned untouched, and so is every field that is not a secret: status,
    status_code, err, resources and the rest of each operation's metadata are what LXD sent.
    """
    if path.lowered[:1] != _OPERATIONS_ROOT:
        return data
    return _scrub(data)
