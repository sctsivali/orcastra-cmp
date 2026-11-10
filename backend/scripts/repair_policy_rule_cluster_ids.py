"""Rewrite tenant policy rules that name a cluster by a superseded registration id.

Deployment data, not schema: an Alembic migration would carry one installation's aliases into
every other. The image does not ship ``scripts/``; feed the file to the running backend:

    docker exec -i <backend> python - --map STALE=REGISTERED [--apply] \
        < backend/scripts/repair_policy_rule_cluster_ids.py

Each ``--map`` names a cluster registered again under a new id; rules naming the old id are
inert (no match in ``get_visible_projects`` or the tenant ACL). A rewrite that
collides with the policy's row for the new id merges the permissions into it and deletes the
stale row, so the unique constraint on ``(policy_id, cluster_id, project_name, instance_name)``
cannot fire. Dry run unless ``--apply``, which writes in one transaction; ``--check-registry``
refuses a mapping target that is not a registered cluster. A second run selects nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Dict, List, Sequence

from sqlalchemy import select

# The stale spelling on the left, the registered id on the right; filled from --map.
MAPPING: Dict[str, str] = {}


def _pair(value: str) -> tuple[str, str]:
    stale, sep, registered = value.partition("=")
    if not sep or not stale.strip() or not registered.strip() or stale.strip() == registered.strip():
        raise argparse.ArgumentTypeError("expected STALE=REGISTERED with two different cluster ids")
    return stale.strip(), registered.strip()


def _print_table(rows: Sequence[Sequence[object]], header: Sequence[str]) -> None:
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(header))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


async def _registered_ids() -> List[str]:
    from app.services.lxd_service import get_lxd_service

    read = get_lxd_service().cert_manager.read_registry(True)
    if read.source is None:
        raise SystemExit("the cluster registry could not be read; refusing to judge the mapping")
    return sorted(read.cluster_ids)


async def run(apply: bool, check_registry: bool) -> int:
    from app.core.database import async_session_maker
    from app.models.db_models import OrganizationCluster, TenantAccessPolicy, TenantPolicyRule

    print("mapping (stale id -> registered id):")
    _print_table([[k, v] for k, v in MAPPING.items()], ["stale", "registered"])
    print()

    if check_registry:
        registered = await _registered_ids()
        missing = [v for v in MAPPING.values() if v not in registered]
        if missing:
            raise SystemExit(f"mapping targets not registered: {missing}; registered: {registered}")
        print(f"registry check: every target is registered ({', '.join(registered)})")
        print()

    async with async_session_maker() as db:
        stale = (
            await db.execute(
                select(TenantPolicyRule, TenantAccessPolicy)
                .join(TenantAccessPolicy, TenantAccessPolicy.id == TenantPolicyRule.policy_id)
                .where(TenantPolicyRule.cluster_id.in_(list(MAPPING)))
                .order_by(TenantPolicyRule.cluster_id, TenantAccessPolicy.organization_id, TenantPolicyRule.id)
            )
        ).all()

        if not stale:
            print("nothing to rewrite: no rule names a stale cluster id")
        else:
            _print_table(
                [
                    [
                        rule.id,
                        rule.cluster_id,
                        MAPPING[rule.cluster_id],
                        policy.id,
                        policy.organization_id,
                        policy.name,
                        rule.project_name,
                        rule.instance_name,
                        ",".join(rule.permissions or []),
                    ]
                    for rule, policy in stale
                ],
                ["rule", "stale", "new", "policy", "org", "policy name", "project", "instance", "permissions"],
            )
            print(f"\n{len(stale)} rule(s) to rewrite")

        # Rules naming an id that is in neither the mapping nor (when asked) the registry are
        # listed and left alone; deciding what they mean is not this script's job.
        others = (
            await db.execute(
                select(TenantPolicyRule.cluster_id)
                .where(TenantPolicyRule.cluster_id.not_in(list(MAPPING)))
                .distinct()
            )
        ).scalars().all()
        if check_registry:
            registered = await _registered_ids()
            unknown = sorted(c for c in others if c not in registered)
            print(f"left alone, not registered and not in the mapping: {unknown or 'none'}")

        bindings = (
            await db.execute(
                select(OrganizationCluster.cluster_id).where(OrganizationCluster.cluster_id.in_(list(MAPPING)))
            )
        ).scalars().all()
        print(f"organization_clusters rows naming a stale id: {sorted(set(bindings)) or 'none (no change needed)'}")

        if not apply or not stale:
            print("\ndry run, nothing written" if not apply else "")
            return 0

        rewritten = merged = 0
        for rule, policy in stale:
            new_id = MAPPING[rule.cluster_id]
            twin = (
                await db.execute(
                    select(TenantPolicyRule).where(
                        TenantPolicyRule.policy_id == rule.policy_id,
                        TenantPolicyRule.cluster_id == new_id,
                        TenantPolicyRule.project_name == rule.project_name,
                        TenantPolicyRule.instance_name == rule.instance_name,
                    )
                )
            ).scalar_one_or_none()
            if twin is not None:
                twin.permissions = sorted(set(twin.permissions or []) | set(rule.permissions or []))
                await db.delete(rule)
                merged += 1
            else:
                rule.cluster_id = new_id
                rewritten += 1
        await db.commit()
        print(f"\napplied: {rewritten} rewritten, {merged} merged into an existing row")

        left = (
            await db.execute(select(TenantPolicyRule.id).where(TenantPolicyRule.cluster_id.in_(list(MAPPING))))
        ).scalars().all()
        print(f"stale rules remaining after apply: {len(left)}")
        return 0 if not left else 1


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the rewrite; default is a dry run")
    parser.add_argument("--check-registry", action="store_true", help="refuse when a mapping target is not a registered cluster")
    parser.add_argument("--map", dest="pairs", type=_pair, action="append", required=True,
                        metavar="STALE=REGISTERED", help="a superseded cluster id and the id it is registered under now")
    args = parser.parse_args(argv)
    MAPPING.update(dict(args.pairs))
    return asyncio.run(run(apply=args.apply, check_registry=args.check_registry))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
