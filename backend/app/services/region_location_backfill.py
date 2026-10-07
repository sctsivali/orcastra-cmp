"""Bring stored cluster locations in line with the region catalogue.

A cluster keeps its own copy of its location in Vault metadata, so a catalogue correction (a
moved point, a renamed regency, a retired id) reaches it only when the location is saved again.
This finds every stored location the catalogue disagrees with and, with --apply, rewrites it the
way PATCH /clusters/{id}/location does: catalogue fields replaced, facility fields kept, zone
re-synced only when it still held the old city or region name, caches dropped, and one audit
event per cluster with the location before and after.

Deployment data, not schema, so it is not a migration. Run inside the backend container,
dry-run by default:

    python -m app.services.region_location_backfill            # list what would change
    python -m app.services.region_location_backfill --apply    # rewrite

Idempotent: once applied, a second run finds nothing to change. Locations whose region id the
catalogue does not know (an API client's own) are listed and left alone.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services.region_catalog import RegionCatalog, get_region_catalog

AUDIT_ACTION = "cluster.location.update"


@dataclass
class LocationChange:
    cluster_id: str
    before: Dict[str, Any]
    after: Dict[str, Any]
    fields: List[str]
    zone: Optional[str]  # the re-synced zone, or None when the stored zone is the operator's own


@dataclass
class BackfillPlan:
    catalog_version: str
    changes: List[LocationChange] = field(default_factory=list)
    up_to_date: List[str] = field(default_factory=list)
    no_location: List[str] = field(default_factory=list)
    unknown_region: List[Tuple[str, str]] = field(default_factory=list)
    unreadable: List[Tuple[str, str]] = field(default_factory=list)


def _metadata_path(cert_manager: Any, cluster_id: str) -> str:
    return f"{cert_manager.clusters_path}/{cluster_id}/metadata"


def _read_metadata(cert_manager: Any, cluster_id: str) -> Dict[str, Any]:
    # note: uncached. A plan judged against a cached body could report a cluster up to date
    # that is not, or rewrite one from a stale read.
    return cert_manager.vault.vault_read(_metadata_path(cert_manager, cluster_id), use_cache=False) or {}


def _change_for(cluster_id: str, metadata: Dict[str, Any], catalog: RegionCatalog) -> Optional[LocationChange]:
    before = metadata.get("location") or {}
    after = catalog.canonical_location(before)
    fields = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    if not fields:
        return None
    zone = metadata.get("zone")
    synced = zone in (None, "", before.get("city"), before.get("region_name"))
    return LocationChange(cluster_id, before, after, fields, after["city"] if synced else None)


def plan(cert_manager: Any, catalog: Optional[RegionCatalog] = None) -> BackfillPlan:
    catalog = catalog or get_region_catalog()
    read = cert_manager.read_registry(use_cache=False)
    if read.source is None:
        raise RuntimeError("the cluster registry could not be read from Vault; nothing was judged")
    result = BackfillPlan(catalog_version=catalog.version)
    for cluster_id in sorted(read.cluster_ids):
        try:
            metadata = _read_metadata(cert_manager, cluster_id)
        except Exception as exc:  # noqa: BLE001 - one unreadable cluster must not hide the rest
            result.unreadable.append((cluster_id, str(exc)))
            continue
        location = metadata.get("location") or {}
        region_id = location.get("region_id")
        if not region_id:
            result.no_location.append(cluster_id)
        elif catalog.resolve(region_id) is None:
            result.unknown_region.append((cluster_id, region_id))
        else:
            change = _change_for(cluster_id, metadata, catalog)
            if change is None:
                result.up_to_date.append(cluster_id)
            else:
                result.changes.append(change)
    return result


def apply(cert_manager: Any, backfill: BackfillPlan, catalog: Optional[RegionCatalog] = None,
          cache: Any = None, activity: Any = None) -> List[str]:
    """Write every planned change. Returns the ids actually rewritten.

    Each cluster is re-read and re-judged just before its write, so a location an operator
    changed after the plan was printed is judged on what is stored now, not overwritten with
    a stale decision.
    """
    catalog = catalog or get_region_catalog()
    written: List[str] = []
    for planned in backfill.changes:
        metadata = _read_metadata(cert_manager, planned.cluster_id)
        change = _change_for(planned.cluster_id, metadata, catalog)
        if change is None:
            continue
        cert_manager.save_cluster_metadata(
            cluster_id=change.cluster_id,
            endpoint=metadata.get("endpoint", ""),
            display_name=metadata.get("display_name", change.cluster_id),
            zone=change.zone if change.zone is not None else metadata.get("zone", ""),
            # Preserve whatever is stored, including "not determined yet", as the PATCH does.
            cluster_type=metadata.get("type"),
            merchant_id=metadata.get("merchant_id"),
            location=change.after,
        )
        written.append(change.cluster_id)
        if cache is not None:
            cache.invalidate_cluster_cache(change.cluster_id)
        if activity is not None:
            activity.log_activity(
                action=AUDIT_ACTION,
                target_type="cluster",
                target_id=change.cluster_id,
                details={
                    "source": "region catalogue backfill",
                    "catalog_version": backfill.catalog_version,
                    "fields": change.fields,
                    "zone_updated_to": change.zone,
                },
                before_state=change.before,
                after_state=change.after,
            )
    if written and cache is not None:
        cache.invalidate_resource_cache("clusters", "regions")
    return written


def _print_table(rows: Sequence[Sequence[object]], header: Sequence[str]) -> None:
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    print("  ".join(str(h).ljust(widths[i]) for i, h in enumerate(header)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def _describe(change: LocationChange) -> List[object]:
    b, a = change.before, change.after
    moved = ""
    if "latitude" in change.fields or "longitude" in change.fields:
        moved = f"({b.get('latitude')}, {b.get('longitude')}) -> ({a['latitude']}, {a['longitude']})"
    return [
        change.cluster_id,
        f"{b.get('region_id')} -> {a['region_id']}" if b.get("region_id") != a["region_id"] else a["region_id"],
        f"{b.get('region_name')} -> {a['region_name']}" if b.get("region_name") != a["region_name"] else a["region_name"],
        moved,
        ",".join(change.fields),
        change.zone if change.zone is not None else "(kept)",
    ]


CONTAINER_STDOUT = "/proc/1/fd/1"


def _route_audit_to_the_container_log() -> None:
    """Make this process's audit records land where the server's do.

    important: run through `docker compose exec`, this is a second process. Without
    setup_logging the audit logger has no handler and Python drops INFO records outright, and
    even with it the records would reach only the exec session's terminal. Fluent Bit ships the
    container's own stdout, which is PID 1's, so the records are written there as well. The
    task journal copy needs the server's event loop and is not available here.
    """
    import logging

    from app.core.config import get_settings
    from app.core.logging_config import setup_logging

    settings = get_settings()
    setup_logging(service_name="orcastra-backend", version=settings.app_version or "0.0.0",
                  log_level="INFO", json_logs=not settings.debug)
    try:
        stream = open(CONTAINER_STDOUT, "w", buffering=1)  # noqa: SIM115 - lives for the process
    except OSError:
        print(f"warning: {CONTAINER_STDOUT} is not writable; audit records go to this terminal only")
        return
    root = logging.getLogger()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(root.handlers[0].formatter if root.handlers else None)
    logging.getLogger("orcastra.audit").addHandler(handler)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Align stored cluster locations with the region catalogue.")
    parser.add_argument("--apply", action="store_true", help="rewrite the locations listed (default: dry run)")
    args = parser.parse_args(argv)

    from app.core.cache import get_cache_manager
    from app.services.activity_logger import get_activity_logger
    from app.services.lxd_service import get_lxd_service

    cert_manager = get_lxd_service().cert_manager
    catalog = get_region_catalog()
    backfill = plan(cert_manager, catalog)

    print(f"region catalogue {catalog.version}")
    if backfill.changes:
        _print_table([_describe(c) for c in backfill.changes], ["cluster", "region id", "region", "point", "fields", "zone"])
    print(f"\n{len(backfill.changes)} to change, {len(backfill.up_to_date)} up to date, "
          f"{len(backfill.no_location)} without a location")
    for cluster_id, region_id in backfill.unknown_region:
        print(f"left alone: {cluster_id} uses region id {region_id!r}, which the catalogue does not know")
    for cluster_id, error in backfill.unreadable:
        print(f"UNREADABLE: {cluster_id}: {error}")

    if not args.apply:
        print("\ndry run, nothing written" if backfill.changes else "")
        return 1 if backfill.unreadable else 0
    _route_audit_to_the_container_log()
    written = apply(cert_manager, backfill, catalog, get_cache_manager(), get_activity_logger())
    print(f"\nrewrote {len(written)} location(s): {', '.join(written) or 'none'}")
    return 1 if backfill.unreadable else 0


if __name__ == "__main__":
    sys.exit(main())
