"""What an API key may make the hypervisor fetch or send: the trusted catalogues and its own clusters.

An instance, image or custom volume create (and a rebuild) can name a source the daemon fetches
itself: an image server, a URL, or another daemon's migration operation. Through the raw proxy that
was a request forgery from the hypervisor to any address it can reach, and a way to feed a crafted
image or instance to an unpatched daemon (demonstrated on the Incus lab, 2026-11-08; see
tests/test_a_key_cannot_make_the_cluster_fetch_from_anywhere.py). A key may name only the
servers the image picker offers (services/remote_image_catalog.py) or a cluster registered in this
dashboard (an image copied between its own clusters), and never a URL, migration or conversion
source. Migrations go through Migration Center, which runs its own preflight.

The same holds the other way round: a body `target` is a host the daemon sends to (a push
migration's operation, an image export's receiver, an Incus backup's S3 URL), so a key may not
name one. The rule reads the body, not the path: a path list missed rebuild (security review,
2026-11-08), and no LXD or Incus endpoint takes a `source` or `target` object it does not act on.
A move between members of one cluster names the member in the query, not in the body.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from fastapi import HTTPException

# Sources the daemon fetches from, or receives from, somewhere the dashboard did not choose.
_REFUSED_SOURCE_TYPES = {"url", "migration", "conversion"}

_TARGET_REFUSAL = (
    "An API key cannot name a target host: the cluster would send an instance, volume, image or "
    "backup to a host the dashboard did not choose. Use Migration Center to move instances."
)


def _target_refusal(target: Any) -> Optional[str]:
    if isinstance(target, Mapping):
        return _TARGET_REFUSAL if target else None
    # An alias's target is a fingerprint; an image export's is the receiving server's URL.
    if isinstance(target, str) and "://" in target:
        return _TARGET_REFUSAL
    return None


def _source_refusal(source: Any) -> Optional[str]:
    if not isinstance(source, Mapping):
        return None
    kind = str(source.get("type") or "").strip().lower()
    if kind in _REFUSED_SOURCE_TYPES:
        return (
            f"An API key cannot create from a {kind} source: the cluster would fetch or accept data "
            "from a host the dashboard did not choose. Use Migration Center to move instances."
        )
    server = source.get("server")
    if server:
        from app.services.remote_image_catalog import image_server_refusal

        refusal = image_server_refusal(str(server))
        if refusal and not _operator_allowed(str(server)) and not _is_registered_cluster(str(server)):
            return f"{refusal}, or a cluster registered in this dashboard."
    return None


def _operator_allowed(server: str) -> bool:
    """A host the operator admitted with KEY_IMAGE_SERVERS_ALLOW, over https."""
    from app.core.config import get_settings

    parsed = urlparse(server)
    allowed = {h.strip().lower() for h in (get_settings().key_image_servers_allow or "").split(",") if h.strip()}
    return parsed.scheme == "https" and bool(parsed.hostname) and parsed.hostname.lower() in allowed


def _is_registered_cluster(server: str) -> bool:
    """Whether `server` is the https endpoint of a cluster registered here (cached Vault reads).

    Asked only when the server is not a public catalogue. Anything unreadable answers False.
    """
    parsed = urlparse(server)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        # note: the daemon dials `https://host` on 443; only a registered endpoint defaults to 8443.
        wanted = (parsed.hostname.lower(), parsed.port or 443)
        from app.services.lxd_service import get_lxd_service

        manager = get_lxd_service().cert_manager
        for cluster_id in manager.list_clusters():
            metadata = manager.vault.vault_read(f"{manager.clusters_path}/{cluster_id}/metadata", use_cache=True) or {}
            endpoint = urlparse(str(metadata.get("endpoint") or ""))
            if endpoint.hostname and (endpoint.hostname.lower(), endpoint.port or 8443) == wanted:
                return True
    except Exception:  # noqa: BLE001 - not knowing is not a yes
        return False
    return False


def pull_source_refusal(method: str, body: Any) -> Optional[str]:
    """Why a raw-proxy request would make the daemon fetch from or send to an untrusted place."""
    if method.upper() != "POST" or not isinstance(body, Mapping):
        return None
    return _source_refusal(body.get("source")) or _target_refusal(body.get("target"))


def create_params_refusal(params: Any) -> Optional[str]:
    """The same rule for the first-class create_instance, whose params are the create body."""
    return _source_refusal(params.get("source")) if isinstance(params, Mapping) else None


def _refuse(reason: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"error": "pull_source_not_allowed", "message": reason})


def assert_key_pull_source_allowed(method: str, body: Any) -> None:
    reason = pull_source_refusal(method, body)
    if reason:
        raise _refuse(reason)


def assert_key_create_params_allowed(params: Any) -> None:
    reason = create_params_refusal(params)
    if reason:
        raise _refuse(reason)
