"""Drop cluster ids no cluster is registered under from API key grants.

The backend image does not ship ``scripts/``; pipe the file in. Dry run by default, idempotent:

    docker exec -i <backend> python - < backend/scripts/repair_key_grant_cluster_ids.py
    docker exec -i <backend> python - --apply < backend/scripts/repair_key_grant_cluster_ids.py

A grant naming an unregistered id authorises nothing, yet the key reports it as held and its
integration polls it through Vault. Nothing is remapped: a re-registered cluster is a new trust
identity, possibly of another organization. Only reported: add-on keys (the installation owns the
grant) and keys an empty grant would hide from the partner's list (its owner decides). Each write
takes the key's ownership lock, re-reads Vault uncached and evicts the key's caches.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Dict, List, Sequence


def _print_table(rows: Sequence[Sequence[object]], header: Sequence[str]) -> None:
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    print("  ".join(str(h).ljust(widths[i]) for i, h in enumerate(header)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def _print_skipped(skipped: Dict[str, int]) -> None:
    if skipped.get("missing"):
        print(f"listed keys Vault no longer holds: {skipped['missing']} (removed by an earlier release; nothing to fix)")
    if skipped.get("unreadable"):
        print(f"listed keys Vault could not read: {skipped['unreadable']} (run it again; if it stays, check Vault)")


def _registered_ids() -> List[str]:
    from app.services.lxd_service import get_lxd_service

    read = get_lxd_service().cert_manager.read_registry(False)
    # Only a live Vault read may decide what to delete; a last-known copy can be stale.
    if read.source != "vault":
        raise SystemExit(f"the cluster registry was not read from Vault ({read.source}); refusing to judge any grant")
    return sorted(read.cluster_ids)


async def run(apply: bool) -> int:
    from app.core.database import async_session_maker
    from app.routers.integrations import get_vault_integrations_client
    from app.services import key_grant_hygiene

    registered = set(await asyncio.to_thread(_registered_ids))
    print(f"registered clusters: {', '.join(sorted(registered))}\n")
    stale = lambda grant: grant.get("cluster_id") not in registered  # noqa: E731

    vault_client = get_vault_integrations_client()
    key_ids = await asyncio.to_thread(key_grant_hygiene.list_key_ids_strict, vault_client)
    plans = []
    skipped: Dict[str, int] = {}
    for key_id in key_ids:
        metadata, why = await asyncio.to_thread(key_grant_hygiene.read_key_metadata_for_report, vault_client, key_id)
        if metadata is None:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        plan = key_grant_hygiene.plan_cleanup(key_id, metadata, stale)
        if plan is not None:
            plans.append(plan)

    _print_skipped(skipped)
    if not plans:
        print(f"nothing to change: none of {len(key_ids)} key grants names an unregistered cluster")
        return 0

    def verdict(plan) -> str:
        if plan.add_on:
            return "left alone: add-on installation owns its grant"
        if plan.left_empty:
            return "left alone: grant would be empty"
        return "drop"

    _print_table(
        [[p.key_id, p.app_name, ",".join(p.drop), verdict(p)] for p in plans],
        ["key", "app", "unregistered ids", "action"],
    )
    writable = [p for p in plans if p.writable]
    print(f"\n{len(writable)} key(s) to rewrite, {len(plans) - len(writable)} reported only, of {len(key_ids)} keys")

    if not apply:
        print("\ndry run, nothing written")
        return 0

    changed = 0
    for plan in writable:
        async with async_session_maker() as db:
            dropped = await key_grant_hygiene.apply_cleanup(
                db, vault_client, plan.key_id, stale, reason="repair: cluster not registered"
            )
        changed += bool(dropped)
        print(f"  {plan.key_id}: dropped {dropped or 'nothing (changed since the listing)'}")
    print(f"\napplied: {changed} key(s) rewritten")
    return 0


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="rewrite the writable keys; default is a dry run")
    args = parser.parse_args(argv)
    return asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
