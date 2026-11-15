"""Read only: which keys would be refused a project create on a cluster another party holds.

The backend image does not ship ``scripts/``, so feed the file to the running backend's interpreter:

    docker exec -i <backend> python - < backend/scripts/report_project_creates_refused_on_held_clusters.py
    docker exec -i <backend> python - --no-idp < backend/scripts/report_project_creates_refused_on_held_clusters.py
    docker exec -i <backend> python - --audit < backend/scripts/report_project_creates_refused_on_held_clusters.py

A key a Partner created is held to its creator's projects on a cluster another party holds
(services/key_party_bound.py). Creating a project there needs an organization the creator runs that
is bound to the cluster (org_scope.org_to_hold_project_grant); OrcaHub purchases that create a
project through such a key are refused otherwise. For every live key and add-on credential
granted lxd.proxy, and every cluster it is granted, this prints the clusters where the key is
bounded with the verdict:

- ok: the create is recorded to the organization named;
- admin creator: an Admin's key is not bounded (--no-idp leaves this unknown);
- purchase <phase>: the creator reaches the cluster only by a purchase in that phase;
- create refused: no organization the creator runs is bound; the organizations this key owns are
  named, with what binding the first of them would change (the bind preview, without the share
  audit unless --audit);
- unreachable: creates are recorded but the stored grant names projects, not `*`, so the key
  cannot reach the project it created.

Nothing is written (the preview tries its row inside a savepoint it rolls back). Key ids are
printed truncated.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Dict, List, Sequence


def _print_table(rows: Sequence[Sequence[object]], header: Sequence[str]) -> None:
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    print("  ".join(str(h).ljust(widths[i]) for i, h in enumerate(header)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def _proxies(metadata: Dict[str, Any]) -> bool:
    capabilities = metadata.get("capabilities")
    return capabilities is None or "lxd.proxy" in (capabilities or [])


async def _impact(db: Any, organization_id: int, cluster_id: str, audit: bool) -> str:
    from app.services.cluster_bind_preview import preview_bind

    preview = await preview_bind(db, organization_id=organization_id, cluster_id=cluster_id, registrant=None,
                                 granter="report", show_organizations=True, audit=audit)
    parts = []
    if preview.several_organizations_after and not preview.several_organizations_before:
        parts.append("cluster becomes shared")
    if preview.holders_lose_whole_cluster_tools:
        parts.append("holders lose whole-cluster tools")
    if preview.share_audit is not None and preview.share_audit.get("safe") is False:
        parts.append("share audit refuses")
    return "; ".join(parts) or "no change for current holders"


async def run(use_idp: bool, audit: bool) -> int:
    from app.core.api_key_expiry import classify_key
    from app.core.database import async_session_maker
    from app.core.org_scope import org_to_hold_project_grant
    from app.core.sale_term import purchase_phases
    from app.routers.integrations import get_vault_integrations_client
    from app.services import key_grant_hygiene
    from app.services.cluster_parties import clusters_held_by_another_party
    from app.services.creator_role import creator_is_admin
    from app.services.key_party_bound import creator_organizations_owned_by_key

    vault_client = get_vault_integrations_client()
    key_ids = await asyncio.to_thread(key_grant_hygiene.list_key_ids_strict, vault_client)
    rows: List[tuple] = []
    counts: Dict[str, int] = {}
    admin_cache: Dict[str, Any] = {}
    async with async_session_maker() as db:
        from sqlalchemy import select

        from app.models.extension_models import ExtensionInstallation

        installers = {str(k): str(v) for k, v in (await db.execute(
            select(ExtensionInstallation.api_key_id, ExtensionInstallation.installed_by)
        )).all() if k}
        live = 0
        for key_id in key_ids:
            metadata = await asyncio.to_thread(key_grant_hygiene.read_key_metadata, vault_client, key_id)
            if classify_key(metadata) != "active" or not _proxies(metadata):
                continue
            live += 1
            addon = metadata.get("extension_install_id") is not None
            creator = (installers.get(key_id) if addon else None) or str(metadata.get("created_by") or "")
            phases = await purchase_phases(db, creator)
            for grant in metadata.get("cluster_permissions") or []:
                if not isinstance(grant, dict):
                    continue
                cluster_id = str(grant.get("cluster_id") or "")
                if cluster_id not in await clusters_held_by_another_party(db, creator, {cluster_id}):
                    continue
                if use_idp:
                    if creator not in admin_cache:
                        admin_cache[creator] = await creator_is_admin(creator)
                    if admin_cache[creator]:
                        counts["admin creator"] = counts.get("admin creator", 0) + 1
                        continue
                phase = phases.get(cluster_id)
                holder = await org_to_hold_project_grant(creator, cluster_id, db)
                owned, others = await creator_organizations_owned_by_key(db, creator, key_id)
                named = [f"{name} ({org_id})" for org_id, name in owned]
                stored = list(grant.get("projects") or ["default"])
                impact = ""
                if phase and phase != "active":
                    verdict = f"purchase {phase}"
                elif holder is None:
                    verdict = "create refused"
                    if owned:
                        impact = await _impact(db, owned[0][0], cluster_id, audit)
                elif "*" not in stored:
                    verdict = f"unreachable (recorded to {holder}; grant names {','.join(stored)})"
                else:
                    verdict = f"ok (recorded to {holder})"
                if not use_idp:
                    verdict += "; creator role not asked"
                counts[verdict.split(" (")[0].split(";")[0]] = counts.get(verdict.split(" (")[0].split(";")[0], 0) + 1
                rows.append((
                    key_id[:12] + "…",
                    str(metadata.get("app_name") or metadata.get("name") or ""),
                    "add-on" if addon else "key",
                    creator or "(none)",
                    cluster_id,
                    ", ".join(named) or (f"{others} not owned by this key" if others else "(none)"),
                    verdict,
                    impact,
                ))
        await db.rollback()

    print(f"live proxy keys read: {live} of {len(key_ids)}; identity provider: {'asked' if use_idp else 'not asked'}; "
          f"share audit: {'run' if audit else 'skipped'}\n")
    if rows:
        _print_table(rows, ["key", "app", "kind", "creator", "cluster", "creator runs (this key owns)", "verdict",
                            "binding the first would"])
    else:
        print("no live proxy key is bounded on a cluster another party holds for its creator")
    print()
    for verdict, count in sorted(counts.items()):
        print(f"{verdict}: {count}")
    print("\nFix a 'create refused' row by binding one of the named organizations to the cluster in "
          "Settings > Integrations > Synced Organizations > Manage clusters (or POST /api/v1/organizations/{id}/clusters).")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    raise SystemExit(asyncio.run(run(use_idp="--no-idp" not in args, audit="--audit" in args)))
