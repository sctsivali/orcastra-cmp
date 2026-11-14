"""What an API key may make the hypervisor fetch or send: the trusted catalogues and its own clusters.

important: a create or rebuild can name a source the daemon fetches itself (an image server, a URL,
another daemon's migration). Through the raw proxy that is request forgery from the hypervisor and
a way to feed a crafted image or instance to an unpatched daemon. A key may name only the image
picker's servers (services/remote_image_catalog.py), ORCA's appliance stream, a host in
KEY_IMAGE_SERVERS_ALLOW, or a cluster registered here; never a URL, migration or conversion source. Likewise a body `target` is a host the
daemon sends to (push migration, image export receiver, Incus backup S3 URL), so a key may not name
one. The rule reads the body, not a path list: no LXD or Incus endpoint takes a `source` or `target`
object it does not act on. Migrations go through Migration Center; a move inside one cluster names
the member in the query string.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from fastapi import HTTPException

# Sources the daemon fetches from, or receives from, somewhere the dashboard did not choose.
_REFUSED_SOURCE_TYPES = {"url", "migration", "conversion"}

# important: ORCA's own appliance stream. OrcaHub pre-caches appliances on a cluster and creates an order's
# instance through an API key naming this server, so on a deployment that never set KEY_IMAGE_SERVERS_ALLOW
# every ORCA download and every order for an appliance the cluster does not hold yet was refused (OrcaHub
# production, 14/11/2026). Trusted for keys only and over https only: the image picker's list and its
# engine check (services/remote_image_catalog.py) are unchanged.
TRUSTED_KEY_IMAGE_HOSTS = frozenset({"images.orca.id"})

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
        # The catalogue check reads the host alone; a key naming a catalogue host on another port would make
        # the hypervisor open TLS to any port there. Only a registered cluster is reached on its own port.
        if refusal is None and (not _default_https_port(str(server)) or "@" in urlparse(str(server)).netloc):
            refusal = "Invalid image_server: a catalogue is reached on 443, without credentials"
        if refusal and not _key_host_allowed(str(server)) and not _is_registered_cluster(str(server)):
            return f"{refusal}, or a cluster registered in this dashboard."
    return None


def _default_https_port(server: str) -> bool:
    """No port, or 443. A port that does not parse is not a default one."""
    try:
        return urlparse(server).port in (None, 443)
    except ValueError:
        return False


def _key_host_allowed(server: str) -> bool:
    """ORCA's appliance stream, or a host the operator admitted with KEY_IMAGE_SERVERS_ALLOW, over https on 443."""
    from app.core.config import get_settings

    parsed = urlparse(server)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    if not _default_https_port(server):
        return False
    host = parsed.hostname.lower()
    if host in TRUSTED_KEY_IMAGE_HOSTS:
        return True
    allowed = {h.strip().lower() for h in (get_settings().key_image_servers_allow or "").split(",") if h.strip()}
    return host in allowed


def _is_registered_cluster(server: str) -> bool:
    """Whether `server` is the https endpoint of a cluster registered here (cached Vault reads).

    Asked only when the server is not a public catalogue. Anything unreadable answers False.
    """
    parsed = urlparse(server)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        # The daemon dials `https://host` on 443; only a registered endpoint defaults to 8443.
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
