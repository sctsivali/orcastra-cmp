"""Read only: what the view-only `default` rule refuses for keys, schedules and tenant rules.

Deployment data, not schema. The backend image does not ship ``scripts/``, so feed the file to the
running backend's interpreter:

    docker exec -i <backend> python - < backend/scripts/report_default_grants_on_held_clusters.py
    docker exec -i <backend> python - --no-idp < backend/scripts/report_default_grants_on_held_clusters.py

`default` on a cluster another party also holds is view only for a Partner, and since the RC5
follow-up round for a key or a snapshot schedule a Partner created (services/key_default_view_only.py,
schedule_authority.default_view_only_refusal). This lists the live keys and active schedules that
hold `default` (or `*`) on such a cluster, with the creator, the creator's role and the verdict:
refused, admin-exempt, or unknown when Authentik did not answer or --no-idp was given. It also
lists the tenant rules naming `default` or `*` on a cluster several organizations hold, which a
Partner can no longer write; existing ones are left in place and reported here.

Nothing is written. Key ids are printed truncated.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Dict, List, Optional, Sequence


def _print_table(rows: Sequence[Sequence[object]], header: Sequence[str]) -> None:
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    print("  ".join(str(h).ljust(widths[i]) for i, h in enumerate(header)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def _reaches_default(grant: Dict[str, Any]) -> bool:
    projects = grant.get("projects") or ["default"]
    return "default" in projects or "*" in projects


async def _verdict(db: Any, email: str, cluster_id: str, use_idp: bool, cache: Dict) -> Optional[str]:
    """None when no other party holds the cluster for this creator, else the rule's answer."""
    from app.services.cluster_parties import default_view_only_clusters
    from app.services.creator_role import creator_is_admin

    key = (email.lower(), cluster_id)
    if key in cache:
        return cache[key]
    if cluster_id not in await default_view_only_clusters(db, email, {cluster_id}):
        cache[key] = None
        return None
    if not use_idp:
        cache[key] = "unknown (--no-idp)"
        return cache[key]
    admin = await creator_is_admin(email)
    cache[key] = "admin-exempt" if admin else ("unknown (IdP did not answer)" if admin is None else "refused")
    return cache[key]


async def _installers(db: Any) -> Dict[str, str]:
    from sqlalchemy import select

    from app.models.extension_models import ExtensionInstallation

    rows = (await db.execute(select(ExtensionInstallation.api_key_id, ExtensionInstallation.installed_by))).all()
    return {str(key_id): str(installed_by) for key_id, installed_by in rows if key_id}


async def run(use_idp: bool) -> int:
    from sqlalchemy import func, select

    from app.core.api_key_expiry import classify_key
    from app.core.database import async_session_maker
    from app.models.db_models import OrganizationCluster, TenantAccessPolicy, TenantPolicyRule
    from app.models.snapshot_schedule_models import SnapshotSchedule
    from app.routers.integrations import get_vault_integrations_client
    from app.services import key_grant_hygiene

    vault_client = get_vault_integrations_client()
    key_ids = await asyncio.to_thread(key_grant_hygiene.list_key_ids_strict, vault_client)
    cache: Dict = {}
    key_rows: List[tuple] = []
    schedule_rows: List[tuple] = []
    async with async_session_maker() as db:
        installers = await _installers(db)
        live = 0
        for key_id in key_ids:
            metadata = await asyncio.to_thread(key_grant_hygiene.read_key_metadata, vault_client, key_id)
            if classify_key(metadata) != "active":
                continue
            live += 1
            creator = installers.get(key_id) if metadata.get("extension_install_id") is not None else None
            creator = creator or str(metadata.get("created_by") or "")
            for grant in metadata.get("cluster_permissions") or []:
                if not isinstance(grant, dict) or not _reaches_default(grant):
                    continue
                cluster_id = str(grant.get("cluster_id") or "")
                verdict = await _verdict(db, creator, cluster_id, use_idp, cache)
                if verdict is not None:
                    key_rows.append((
                        key_id[:12] + "…",
                        str(metadata.get("app_name") or metadata.get("name") or ""),
                        "add-on" if metadata.get("extension_install_id") is not None else "key",
                        creator or "(none)",
                        cluster_id,
                        ",".join(grant.get("permissions") or []),
                        verdict,
                    ))

        schedules = (await db.execute(
            select(SnapshotSchedule).where(SnapshotSchedule.project == "default", SnapshotSchedule.state == "active")
        )).scalars().all()
        for schedule in schedules:
            verdict = await _verdict(db, schedule.owner_email or "", schedule.cluster_id, use_idp, cache)
            if verdict is not None:
                schedule_rows.append((schedule.id, schedule.instance_name, schedule.cluster_id,
                                      schedule.owner_email, verdict))

        shared = select(OrganizationCluster.cluster_id).group_by(OrganizationCluster.cluster_id).having(
            func.count(OrganizationCluster.id) > 1
        )
        rules = (await db.execute(
            select(TenantPolicyRule.id, TenantAccessPolicy.organization_id, TenantPolicyRule.cluster_id,
                   TenantPolicyRule.project_name, TenantPolicyRule.instance_name)
            .join(TenantAccessPolicy, TenantAccessPolicy.id == TenantPolicyRule.policy_id)
            .where(TenantPolicyRule.project_name.in_(("default", "*")), TenantPolicyRule.cluster_id.in_(shared))
            .order_by(TenantPolicyRule.cluster_id, TenantPolicyRule.id)
        )).all()

    print(f"live keys read: {live} of {len(key_ids)}; identity provider: {'asked' if use_idp else 'not asked'}\n")
    if key_rows:
        _print_table(key_rows, ["key", "app", "kind", "creator", "cluster", "level", "verdict"])
    else:
        print("no live key holds default on a cluster another party holds for its creator")
    print()
    if schedule_rows:
        _print_table(schedule_rows, ["schedule", "instance", "cluster", "owner", "verdict"])
    else:
        print("no active schedule in default on a cluster another party holds for its owner")
    print()
    if rules:
        _print_table([tuple(rule) for rule in rules], ["rule", "organization", "cluster", "project", "instance"])
        print(f"\n{len(rules)} tenant rule(s) name default or * on a cluster several organizations hold")
    else:
        print("no tenant rule names default or * on a cluster several organizations hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(use_idp="--no-idp" not in sys.argv[1:])))
