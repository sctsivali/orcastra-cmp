"""
Remote simplestreams image catalogues, aggregated for the create form's Custom and Ubuntu tabs,
and the allowlist an `image_server` handed to a daemon must pass. Moved out of the images router,
which owns the store reads; nothing here touches a cluster.

Each server lists the engines that can pull from it. LXD reads Canonical's two servers. Incus
reads images.linuxcontainers.org only: its simplestreams reader (shared/simplestreams/products.go,
v6.0.7 and v7.6.0) takes `incus.tar.xz` or `incus_combined.tar.gz` metadata and nothing else, and
cloud-images.ubuntu.com ships `lxd.tar.xz`. Ubuntu for Incus comes from linuxcontainers
(`ubuntu/24.04` and so on).
"""

import json
import logging
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, List, Optional
from urllib.parse import urlparse

from fastapi import HTTPException

from app.core.cache import get_cache_manager
from app.core.type_utils import normalize_node_type, resolve_cluster_type

logger = logging.getLogger(__name__)

# Remote simplestreams catalogs to aggregate, and the engines each one serves.
REMOTE_IMAGE_SERVERS = [
    {"server": "https://images.lxd.canonical.com", "source": "Custom", "engines": ("lxd",)},
    {"server": "https://cloud-images.ubuntu.com/releases", "source": "Ubuntu", "engines": ("lxd",)},
    {"server": "https://images.linuxcontainers.org", "source": "Custom", "engines": ("incus",)},
]
_ENGINE_LABELS = {"lxd": "LXD", "incus": "Incus"}
# note: v2 since rows carry `engines` and the Incus server; the old key held LXD's rows only.
REMOTE_CATALOG_CACHE_KEY = "images:remote:v2"
# Metadata files whose combined_* fields name the image fingerprints, per engine family.
_META_FTYPES = ("lxd.tar.xz", "incus.tar.xz")


def _family(engine: Optional[str]) -> Optional[str]:
    """lxd or incus; MicroCloud runs LXD. None when the engine is not known."""
    return normalize_node_type(engine).replace("microcloud", "lxd") if engine else None


def servers_for(engine: Optional[str]) -> List[dict]:
    """The servers `engine` can pull from; every server when the engine is not known."""
    family = _family(engine)
    return [s for s in REMOTE_IMAGE_SERVERS if family is None or family in s["engines"]]


def image_server_refusal(image_server: Optional[str], engine: Optional[str] = None) -> Optional[str]:
    """Why `image_server` may not be handed to a daemon of `engine`, or None when it may.

    important: the host allowlist is the SSRF guard. The daemon fetches from this URL itself, and
    creating needs only Partner, so anything off the list could point it at internal or metadata
    endpoints. The engine check only turns a later daemon failure into a clear early refusal.
    """
    if not image_server:
        return None
    parsed = urlparse(image_server)
    known = {urlparse(s["server"]).hostname: s for s in REMOTE_IMAGE_SERVERS}
    if parsed.scheme != "https" or parsed.hostname not in known:
        return "Invalid image_server. Allowed remote image servers: " + ", ".join(sorted(h for h in known if h))
    family = _family(engine)
    if family is not None and family not in known[parsed.hostname]["engines"]:
        usable = sorted({urlparse(s["server"]).hostname for s in servers_for(family)})
        return (
            f"{parsed.hostname} does not serve {_ENGINE_LABELS.get(family, family)} images. "
            f"Use {', '.join(usable)}."
        )
    return None


def target_engine(lxd_service: object, host: str) -> Optional[str]:
    """What `host` last told its pooled client it is, without a round trip.

    None when nothing trusted is pooled (or there is no pool): then only the SSRF allowlist
    applies and the daemon itself reports a server it cannot read.
    """
    peek = getattr(getattr(lxd_service, "connection_manager", None), "peek_client", None)
    return resolve_cluster_type(None, peek(host)) if callable(peek) else None


def validate_image_server(image_server: Optional[str], engine: Optional[str] = None) -> None:
    """Refuse (400) an image_server the daemon must not be pointed at; see image_server_refusal."""
    reason = image_server_refusal(image_server, engine)
    if reason:
        raise HTTPException(status_code=400, detail=reason)


def rows_for_engine(rows: Iterable[dict], engine: Optional[str]) -> List[dict]:
    """Catalogue rows `engine` can pull. A row without `engines` predates them and is LXD's."""
    family = _family(engine)
    return [r for r in rows if family is None or family in (r.get("engines") or ["lxd"])]
# Upper bound for a cold remote-catalog fetch (per-remote urllib timeouts can sum
# past a minute). On timeout we return an empty, uncached result.
_REMOTE_FETCH_TIMEOUT = 60
# Fallback ftypes (older streams without combined_* fingerprints).
_CONTAINER_FTYPES = {"squashfs", "root.squashfs", "rootfs.squashfs", "root.tar.xz"}
_VM_FTYPES = {"disk.qcow2", "disk-kvm.img", "disk1.img"}


def _parse_simplestreams(server: str, source: str, engines: Iterable[str] = ("lxd",)) -> List[dict]:
    """
    Fetch + parse one simplestreams catalog into rows, one per
    (distribution, release, variant, architecture, type). Includes the LXD
    image fingerprint per type (combined_*_sha256) so the UI can flag images
    already cached on a node. Blocking (run in a thread).
    """
    base = server.rstrip("/") + "/"
    index = json.load(urllib.request.urlopen(base + "streams/v1/index.json", timeout=15))
    # Prefer the image-downloads product file (LXD-usable images).
    path = None
    for key, entry in (index.get("index") or {}).items():
        if entry.get("datatype") == "image-downloads" or key == "images":
            path = entry.get("path")
            break
    if not path:
        entries = list((index.get("index") or {}).values())
        path = entries[0].get("path") if entries else None
    if not path:
        return []

    data = json.load(urllib.request.urlopen(base + path, timeout=90))
    rows: List[dict] = []
    for _key, product in (data.get("products") or {}).items():
        versions = product.get("versions") or {}
        if not versions:
            continue
        items = versions[sorted(versions.keys())[-1]].get("items") or {}
        lxd_meta = next((items[f] for f in _META_FTYPES if f in items), None) or {}
        fp_container = lxd_meta.get("combined_squashfs_sha256")
        fp_vm = lxd_meta.get("combined_disk-kvm-img_sha256")
        ftypes = set(items.keys())
        has_container = bool(fp_container) or bool(ftypes & _CONTAINER_FTYPES)
        has_vm = bool(fp_vm) or bool(ftypes & _VM_FTYPES)
        if not (has_container or has_vm):
            continue
        aliases = [a.strip() for a in (product.get("aliases") or "").split(",") if a.strip()]
        if not aliases:
            continue
        alias = aliases[0]
        os_ = (product.get("os") or "").strip()
        distribution = (os_[:1].upper() + os_[1:]) if os_ else source
        release = (product.get("version") or product.get("release") or "").strip()
        variant = (product.get("variant") or product.get("release") or "default").strip()
        arch = product.get("arch") or ""
        base_row = {
            "distribution": distribution,
            "release": release,
            "variant": variant,
            "architecture": arch,
            "alias": alias,
            "source": source,
            "server": server,
            "engines": list(engines),
        }
        if has_container:
            rows.append({**base_row, "type": "container", "fingerprint": fp_container or ""})
        if has_vm:
            rows.append({**base_row, "type": "virtual-machine", "fingerprint": fp_vm or ""})
    return rows


def _fetch_remote_catalog() -> List[dict]:
    """Aggregate all configured remote catalogs (best-effort per remote).

    The remotes are fetched concurrently (the Ubuntu catalog is ~17 MB / tens of
    seconds, so we overlap it with the smaller community fetch).
    """
    rows: List[dict] = []
    with ThreadPoolExecutor(max_workers=len(REMOTE_IMAGE_SERVERS)) as pool:
        futures = {
            pool.submit(_parse_simplestreams, r["server"], r["source"], r["engines"]): r
            for r in REMOTE_IMAGE_SERVERS
        }
        for fut, remote in futures.items():
            try:
                rows.extend(fut.result())
            except Exception as e:
                logger.error(f"Remote catalog {remote['server']} failed: {e}")
    return rows


def prewarm_remote_images_cache() -> None:
    """Populate the remote-image catalog cache (called at startup so the first
    'Select base image' dialog open is instant). Best-effort."""
    try:
        rows = _fetch_remote_catalog()
        get_cache_manager().set("api_responses", REMOTE_CATALOG_CACHE_KEY, rows, ttl=3600)
        logger.info(f"Pre-warmed remote image catalog: {len(rows)} rows")
    except Exception as e:
        logger.error(f"Failed to pre-warm remote image catalog: {e}")


