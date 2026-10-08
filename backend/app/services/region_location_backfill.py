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
    python -m app.services.region_location_backfill --apply --operator "<who runs it>"

Each rewrite is a check-and-set on the record version it judged, and touches only the location
and the zone, so an edit that lands between the read and the write is never reverted.

Idempotent: once applied, a second run finds nothing to change. Locations whose region id the
catalogue does not know (an API client's own) are listed and left alone.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services.region_catalog import RegionCatalog, get_region_catalog

AUDIT_ACTION = "cluster.location.update"
SOURCE = "region catalogue backfill"
# note: a stored name may predate input validation, and the table below goes to a terminal.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _printable(value: object) -> str:
    return _CONTROL.sub("?", str(value))


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
    failed: List[Tuple[str, str]] = field(default_factory=list)  # filled by apply


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


def _rewrite(cert_manager: Any, cluster_id: str, catalog: RegionCatalog) -> Optional[LocationChange]:
    """Rewrite one record with check-and-set. Returns the change written, or None when the record
    no longer needs one.

    important: the write carries the record exactly as it was read with its version, location and
    zone replaced. A PATCH, a rename or a type update landing in between makes the write fail
    instead of being reverted, and the record is judged again once.
    """
    from app.core.vault_client import VaultCasMismatch

    path = _metadata_path(cert_manager, cluster_id)
    for _attempt in range(2):
        metadata, version = cert_manager.vault.vault_read_versioned(path)
        if metadata is None:
            raise RuntimeError("the record is gone; a deleted cluster is never recreated")
        change = _change_for(cluster_id, metadata, catalog)
        if change is None:
            return None
        record = {**metadata, "location": change.after}
        if change.zone is not None:
            record["zone"] = change.zone
        try:
            cert_manager.vault.vault_kv_put(path, record, cas=version)
            return change
        except VaultCasMismatch:
            continue
    raise RuntimeError("the record kept changing while it was being rewritten; run the backfill again")


def apply(cert_manager: Any, backfill: BackfillPlan, catalog: Optional[RegionCatalog] = None,
          cache: Any = None, activity: Any = None, operator: Optional[str] = None) -> List[str]:
    """Write every planned change. Returns the ids actually rewritten; failures go to backfill.failed.

    Each cluster is re-read and re-judged at its write, so a location an operator changed after the
    plan was printed is judged on what is stored now. One failed write is audited and the rest still
    run, and the list caches are dropped for whatever was written.
    """
    catalog = catalog or get_region_catalog()
    written: List[str] = []
    for planned in backfill.changes:
        try:
            change = _rewrite(cert_manager, planned.cluster_id, catalog)
        except Exception as exc:  # noqa: BLE001 - one failed write must not stop the others
            backfill.failed.append((planned.cluster_id, str(exc)))
            if activity is not None:
                activity.log_activity(
                    action=AUDIT_ACTION, target_type="cluster", target_id=planned.cluster_id,
                    result="failure", error_code="BACKFILL_WRITE_FAILED", error_message=str(exc)[:300],
                    details={"source": SOURCE, "catalog_version": backfill.catalog_version, "operator": operator},
                )
            continue
        if change is None:
            continue
        written.append(change.cluster_id)
        if cache is not None:
            cache.invalidate_cluster_cache(change.cluster_id)
        if activity is not None:
            activity.log_activity(
                action=AUDIT_ACTION,
                target_type="cluster",
                target_id=change.cluster_id,
                details={
                    "source": SOURCE,
                    "catalog_version": backfill.catalog_version,
                    "fields": change.fields,
                    "zone_updated_to": change.zone,
                    "operator": operator,
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
    return [_printable(cell) for cell in (
        change.cluster_id,
        f"{b.get('region_id')} -> {a['region_id']}" if b.get("region_id") != a["region_id"] else a["region_id"],
        f"{b.get('region_name')} -> {a['region_name']}" if b.get("region_name") != a["region_name"] else a["region_name"],
        moved,
        ",".join(change.fields),
        change.zone if change.zone is not None else "(kept)",
    )]


CONTAINER_STDOUT = "/proc/1/fd/1"


def _route_audit_to_the_container_log() -> bool:
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
        return False
    root = logging.getLogger()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(root.handlers[0].formatter if root.handlers else None)
    logging.getLogger("orcastra.audit").addHandler(handler)
    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Align stored cluster locations with the region catalogue.")
    parser.add_argument("--apply", action="store_true", help="rewrite the locations listed (default: dry run)")
    parser.add_argument("--operator", default=os.environ.get("SUDO_USER") or os.environ.get("USER") or "",
                        help="who runs the rewrite, recorded in every audit event")
    args = parser.parse_args(argv)
    operator = _printable(args.operator).strip()[:120] or "unknown"

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
        print(f"left alone: {_printable(cluster_id)} uses region id {_printable(repr(region_id))}, "
              "which the catalogue does not know")
    for cluster_id, error in backfill.unreadable:
        print(f"UNREADABLE: {_printable(cluster_id)}: {_printable(error)}")

    if not args.apply:
        print("\ndry run, nothing written" if backfill.changes else "")
        return 1 if backfill.unreadable else 0
    if not _route_audit_to_the_container_log():
        print(f"\nrefusing --apply: {CONTAINER_STDOUT} is not writable, so the audit records would reach this "
              "terminal only. Nothing was written.")
        return 2
    written = apply(cert_manager, backfill, catalog, get_cache_manager(), get_activity_logger(), operator=operator)
    print(f"\nrewrote {len(written)} location(s): {', '.join(written) or 'none'}")
    for cluster_id, error in backfill.failed:
        print(f"NOT REWRITTEN: {_printable(cluster_id)}: {_printable(error)}")
    return 1 if backfill.unreadable or backfill.failed else 0


if __name__ == "__main__":
    sys.exit(main())
