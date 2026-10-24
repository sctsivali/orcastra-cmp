"""Drop cluster ids no cluster is registered under from API key grants.

Deployment data, not schema. The backend image does not ship ``scripts/``, so feed the file to
the running backend's interpreter, dry run by default:

    docker exec -i <backend> python - < backend/scripts/repair_key_grant_cluster_ids.py
    docker exec -i <backend> python - --apply < backend/scripts/repair_key_grant_cluster_ids.py

A grant naming an unregistered id authorises nothing (there is no cluster to reach), but the key
reports it as held, its integration polls it, and every poll looked the id up in Vault. On the
dev installation the owner key of organization 6 still held ``nuc-2`` and ``blitz01``, the ids
of two clusters registered again as ``nuc2`` and ``blitz001``.

Nothing is remapped: a cluster registered again is a new trust identity, and ``blitz001``
belongs to another organization. Two kinds of key are reported and left alone: an add-on
installation's key (the installation owns its grant) and a key whose grant would be left
empty (an empty grant hides the key from its partner's list; its owner decides). Each write
takes the key's ownership lock, re-reads Vault uncached, and evicts the key's caches. Idempotent:
a second run selects nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import List, Sequence


def _print_table(rows: Sequence[Sequence[object]], header: Sequence[str]) -> None:
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    print("  ".join(str(h).ljust(widths[i]) for i, h in enumerate(header)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def _registered_ids() -> List[str]:
    from app.services.lxd_service import get_lxd_service

    read = get_lxd_service().cert_manager.read_registry(False)
    # A last-known copy is fine for a picker, not for deciding what to delete.
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
    for key_id in key_ids:
        metadata = await asyncio.to_thread(key_grant_hygiene.read_key_metadata, vault_client, key_id)
        plan = key_grant_hygiene.plan_cleanup(key_id, metadata, stale)
        if plan is not None:
            plans.append(plan)

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
