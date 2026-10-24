"""Read only: which API key grants reach nothing once grants are bound to registration generations.

Deployment data, not schema. The backend image does not ship ``scripts/``, so feed the file to the
running backend's interpreter:

    docker exec -i <backend> python - < backend/scripts/report_inert_key_grants.py

Run it before enabling generations (services/cluster_generations.py) to see what they will pause,
and after, to see what they paused. Before the backfill has run, the registrations it would adopt
(every id the registry lists that has no registration row) are assumed, so the answer is the same
on either side of the deploy. A grant is reported when the registration it was given for is not
active: its id is not registered, or it was registered again after the grant was written.

Nothing is written. Key ids are printed truncated.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Dict, List, Sequence, Tuple


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
    if read.source != "vault":
        raise SystemExit(f"the cluster registry was not read from Vault ({read.source}); refusing to judge any grant")
    return sorted(read.cluster_ids)


async def _matcher(registered: List[str]) -> Tuple[Callable[[Dict], bool], str]:
    """Whether a grant still reaches its registration, with the backfill's adoptions assumed."""
    from sqlalchemy import inspect as sa_inspect, text

    from app.core.database import async_session_maker, engine

    async with engine.connect() as conn:
        has_table = await conn.run_sync(lambda sync: sa_inspect(sync).has_table("cluster_registrations"))
    listed = set(registered)
    if not has_table:
        # Before the deploy every registered id would be adopted as legacy, which every unstamped
        # grant matches: what pauses is a grant naming an id no cluster is registered under.
        return (lambda grant: grant.get("cluster_id") in listed), (
            "assumed: generations not deployed yet, every registered id adopted as legacy"
        )
    from app.services import cluster_generations as gens

    async with async_session_maker() as db:
        state = await gens._read(db)
        ever = {row[0] for row in (await db.execute(text("select distinct cluster_id from cluster_registrations"))).all()}
    if state.closed:
        return state.matches, "read from cluster_registrations"
    active = dict(state.active)
    for cid in listed - ever:
        active[cid] = gens.Generation(cid, "(adopted)", legacy=True)
    return gens.Generations(active=active, closed=True).matches, (
        "read, with the pending backfill's adoptions assumed"
    )


async def run() -> int:
    from app.routers.integrations import get_vault_integrations_client
    from app.services import key_grant_hygiene

    registered = await asyncio.to_thread(_registered_ids)
    matches, source = await _matcher(registered)
    print(f"registered clusters: {', '.join(registered)}")
    print(f"registrations: {source}\n")

    vault_client = get_vault_integrations_client()
    key_ids = await asyncio.to_thread(key_grant_hygiene.list_key_ids_strict, vault_client)
    rows = []
    for key_id in key_ids:
        metadata = await asyncio.to_thread(key_grant_hygiene.read_key_metadata, vault_client, key_id)
        grants = [g for g in (metadata.get("cluster_permissions") or []) if isinstance(g, dict)]
        inert = [g for g in grants if not matches(g)]
        if not inert:
            continue
        rows.append((
            key_id[:12] + "…",
            str(metadata.get("app_name") or metadata.get("name") or ""),
            ", ".join(str(g.get("cluster_id")) for g in inert),
            f"{len(grants) - len(inert)} of {len(grants)}",
        ))
    if not rows:
        print(f"nothing pauses: every grant of {len(key_ids)} keys reaches its registration")
        return 0
    _print_table(rows, ["key", "app", "paused grants", "grants still live"])
    print(f"\n{len(rows)} of {len(key_ids)} keys hold a grant that reaches nothing")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
