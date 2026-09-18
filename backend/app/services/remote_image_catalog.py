"""
Remote simplestreams image catalogues (LXD's `images:` and `ubuntu:`), aggregated for the
create form's Custom and Ubuntu tabs. Moved out of the images router, which owns the store
reads; nothing here touches a cluster.
"""

import json
import logging
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import List

from app.core.cache import get_cache_manager

logger = logging.getLogger(__name__)

# Remote simplestreams catalogs to aggregate (LXD's `images:` and `ubuntu:`).
REMOTE_IMAGE_SERVERS = [
    {"server": "https://images.lxd.canonical.com", "source": "Custom"},
    {"server": "https://cloud-images.ubuntu.com/releases", "source": "Ubuntu"},
]
# Upper bound for a cold remote-catalog fetch (per-remote urllib timeouts can sum
# past a minute). On timeout we return an empty, uncached result.
_REMOTE_FETCH_TIMEOUT = 60
# Fallback ftypes (older streams without combined_* fingerprints).
_CONTAINER_FTYPES = {"squashfs", "root.squashfs", "rootfs.squashfs", "root.tar.xz"}
_VM_FTYPES = {"disk.qcow2", "disk-kvm.img", "disk1.img"}


def _parse_simplestreams(server: str, source: str) -> List[dict]:
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
        lxd_meta = items.get("lxd.tar.xz") or {}
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
            pool.submit(_parse_simplestreams, r["server"], r["source"]): r
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
        get_cache_manager().set("api_responses", "images:remote:all", rows, ttl=3600)
        logger.info(f"Pre-warmed remote image catalog: {len(rows)} rows")
    except Exception as e:
        logger.error(f"Failed to pre-warm remote image catalog: {e}")


