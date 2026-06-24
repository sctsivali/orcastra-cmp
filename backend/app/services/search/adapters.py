"""Per-resource search adapters.

Each adapter knows how to (a) fetch its candidates - reusing the EXACT same
scoped/cached list path the per-resource router already exposes, so RBAC and
tenant-ACL filtering are never re-implemented (no drift, no leak), (b) declare
which fields to match, and (c) render a result row with an in-app href.

LXD-backed adapters call the router list functions directly: those functions
read the request-scoped org/ACL context vars (set by ``org_scope_dependency``
on the search router) and already drop anything the caller may not see. DB-backed
adapters query Postgres directly with ILIKE.
"""

from dataclasses import dataclass
from typing import Any, List, Optional
from urllib.parse import quote

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.core.config import Settings
from app.core.database import async_session_maker
from app.core.rbac import ROLE_HIERARCHY, Role
from app.models.db_models import AuditLogEntry, LocalIdentity, Tag
from app.models.search_schemas import SearchGroup, SearchResultItem
from app.services.search.scorer import FieldSpec, best_score

# Cap how many DB rows we pull before ranking (LXD lists are already scoped/capped upstream).
_DB_FETCH_CAP = 100


def _qs(path: str, **params: Any) -> str:
    """Build an in-app href with a querystring, skipping empty values."""
    items = [(k, v) for k, v in params.items() if v]
    if not items:
        return path
    return f"{path}?" + "&".join(f"{k}={quote(str(v))}" for k, v in items)


@dataclass
class SearchContext:
    """Everything an adapter needs for one search run."""

    query: str
    per_type_limit: int
    lxd_service: Any
    db: AsyncSession
    user: AuthenticatedUser
    highest_role: Role
    settings: Settings


class ResourceAdapter:
    """Base adapter: fetch -> score -> rank -> cap into a SearchGroup."""

    type: str = ""
    label: str = ""
    min_role: Role = Role.TENANT

    def enabled(self, ctx: SearchContext) -> bool:
        return ROLE_HIERARCHY.get(ctx.highest_role, 0) >= ROLE_HIERARCHY.get(self.min_role, 0)

    async def fetch(self, ctx: SearchContext) -> List[Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def fields(self, candidate: Any) -> List[FieldSpec]:  # pragma: no cover - abstract
        raise NotImplementedError

    def to_item(self, candidate: Any, score: int, field: Optional[str]) -> SearchResultItem:  # pragma: no cover
        raise NotImplementedError

    async def run(self, ctx: SearchContext) -> SearchGroup:
        candidates = await self.fetch(ctx)
        scored = []
        for c in candidates:
            score, field = best_score(ctx.query, self.fields(c))
            if score > 0:
                scored.append((score, field, c))
        scored.sort(key=lambda t: t[0], reverse=True)
        items = [self.to_item(c, s, f) for (s, f, c) in scored[: ctx.per_type_limit]]
        return SearchGroup(type=self.type, label=self.label, total=len(scored), items=items)


# ---------------------------------------------------------------------------
# LXD-backed adapters (reuse the router list functions -> scope + ACL + cache)
# ---------------------------------------------------------------------------


class InstanceAdapter(ResourceAdapter):
    type, label = "instance", "Instances"

    async def fetch(self, ctx):
        from app.routers.instances import list_instances

        # important: pass every Query()-defaulted arg explicitly. Calling a FastAPI
        # endpoint function directly leaves those defaults as FieldInfo objects (not
        # None), which corrupts host/limit handling.
        resp = await list_instances(
            host=None, status=None, instance_type=None, offset=0, limit=0, lxd_service=ctx.lxd_service
        )
        return list(resp.vms)

    def fields(self, vm):
        return [
            (vm.name, 1.0, "name"),
            (" ".join(vm.ip_addresses or []), 0.85, "ip"),
            (" ".join(vm.tags or []), 0.9, "tags"),
            (vm.project, 0.7, "project"),
            (vm.host, 0.7, "host"),
            (vm.description, 0.5, "description"),
        ]

    def to_item(self, vm, score, field):
        status = vm.status.value if hasattr(vm.status, "value") else str(vm.status)
        ip = (vm.ip_addresses or [None])[0]
        parts = [status]
        if ip:
            parts.append(ip)
        if vm.project:
            parts.append(f"project {vm.project}")
        parts.append(f"node {vm.host}")
        return SearchResultItem(
            type=self.type,
            id=f"{vm.host}/{vm.name}",
            title=vm.name,
            subtitle=" · ".join(parts),
            score=score,
            match_field=field,
            href=_qs("/inventory/instances", q=vm.name, host=vm.host),
            meta={"host": vm.host, "project": vm.project, "status": status, "instance_type": vm.type},
        )


class ProjectAdapter(ResourceAdapter):
    type, label = "project", "Projects"

    async def fetch(self, ctx):
        from app.routers.projects import list_all_projects

        resp = await list_all_projects(lxd_service=ctx.lxd_service)
        return [p for plr in resp for p in plr.projects]

    def fields(self, p):
        return [(p.name, 1.0, "name"), (p.description, 0.5, "description")]

    def to_item(self, p, score, field):
        return SearchResultItem(
            type=self.type,
            id=f"{p.host}/{p.name}",
            title=p.name,
            subtitle=f"project · node {p.host}",
            score=score,
            match_field=field,
            href=_qs("/inventory/instances", project=p.name, host=p.host),
            meta={"host": p.host},
        )


class NetworkAdapter(ResourceAdapter):
    type, label = "network", "Networks"

    async def fetch(self, ctx):
        from app.routers.networks import list_networks

        resp = await list_networks(host=None, lxd_service=ctx.lxd_service)
        return list(resp.networks)

    def fields(self, n):
        return [(n.name, 1.0, "name"), (n.type, 0.7, "type"), (n.description, 0.5, "description")]

    def to_item(self, n, score, field):
        return SearchResultItem(
            type=self.type,
            id=f"{n.node}/{n.name}",
            title=n.name,
            subtitle=f"{n.type} · node {n.node}",
            score=score,
            match_field=field,
            href=_qs("/inventory/networks", q=n.name),
            meta={"node": n.node, "type": n.type},
        )


class StoragePoolAdapter(ResourceAdapter):
    type, label = "storage-pool", "Storage Pools"

    async def fetch(self, ctx):
        from app.routers.storage_pools import list_storage_pools

        resp = await list_storage_pools(host=None, lxd_service=ctx.lxd_service)
        return list(resp.pools)

    def fields(self, pool):
        return [(pool.name, 1.0, "name"), (pool.driver, 0.7, "driver"), (pool.description, 0.5, "description")]

    def to_item(self, pool, score, field):
        return SearchResultItem(
            type=self.type,
            id=f"{pool.node}/{pool.name}",
            title=pool.name,
            subtitle=f"{pool.driver} · node {pool.node}",
            score=score,
            match_field=field,
            href=_qs("/inventory/storage-pools", q=pool.name),
            meta={"node": pool.node, "driver": pool.driver},
        )


class StorageVolumeAdapter(ResourceAdapter):
    type, label = "storage-volume", "Storage Volumes"

    async def fetch(self, ctx):
        from app.routers.storage_volumes import list_storage_volumes

        resp = await list_storage_volumes(host=None, pool=None, lxd_service=ctx.lxd_service)
        return list(resp.volumes)

    def fields(self, v):
        return [(v.name, 1.0, "name"), (v.pool, 0.7, "pool"), (v.description, 0.5, "description")]

    def to_item(self, v, score, field):
        return SearchResultItem(
            type=self.type,
            id=f"{v.node}/{v.pool}/{v.name}",
            title=v.name,
            subtitle=f"pool {v.pool} · node {v.node}",
            score=score,
            match_field=field,
            href=_qs("/inventory/storage-volumes", q=v.name),
            meta={"node": v.node, "pool": v.pool},
        )


class ImageAdapter(ResourceAdapter):
    type, label = "image", "Images"

    async def fetch(self, ctx):
        from app.routers.images import list_all_images

        resp = await list_all_images(lxd_service=ctx.lxd_service)
        return [(img, ilr.host) for ilr in resp for img in ilr.images]

    def fields(self, candidate):
        img, _host = candidate
        return [
            (" ".join(img.aliases or []), 0.9, "alias"),
            (img.fingerprint, 0.85, "fingerprint"),
            (img.description, 0.5, "description"),
            (img.architecture, 0.5, "architecture"),
        ]

    def to_item(self, candidate, score, field):
        img, host = candidate
        label = img.aliases[0] if img.aliases else img.fingerprint[:12]
        return SearchResultItem(
            type=self.type,
            id=f"{host}/{img.fingerprint}",
            title=label,
            subtitle=f"{img.architecture} · node {host}",
            score=score,
            match_field=field,
            href=_qs("/inventory/images", q=label),
            meta={"host": host, "fingerprint": img.fingerprint},
        )


class ProfileAdapter(ResourceAdapter):
    type, label = "profile", "Profiles"

    async def fetch(self, ctx):
        from app.routers.profiles import list_profiles

        resp = await list_profiles(host=None, lxd_service=ctx.lxd_service)
        return list(resp.profiles)

    def fields(self, p):
        return [(p.name, 1.0, "name"), (p.description, 0.5, "description")]

    def to_item(self, p, score, field):
        return SearchResultItem(
            type=self.type,
            id=f"{p.node}/{p.name}",
            title=p.name,
            subtitle=f"profile · node {p.node}",
            score=score,
            match_field=field,
            href=_qs("/inventory/profiles", q=p.name),
            meta={"node": p.node},
        )


# ---------------------------------------------------------------------------
# DB-backed adapters (direct ILIKE on Postgres)
# ---------------------------------------------------------------------------


class TagAdapter(ResourceAdapter):
    type, label = "tag", "Tags"

    async def fetch(self, ctx):
        like = f"%{ctx.query.strip()}%"
        stmt = (
            select(Tag)
            .where(or_(Tag.name.ilike(like), Tag.display_name.ilike(like), Tag.description.ilike(like)))
            .limit(_DB_FETCH_CAP)
        )
        # important: own session - adapters run concurrently and an AsyncSession is
        # not safe for concurrent use (sharing ctx.db raises "concurrent operations").
        async with async_session_maker() as db:
            return list((await db.execute(stmt)).scalars().all())

    def fields(self, t):
        return [(t.name, 1.0, "name"), (t.display_name, 0.9, "display_name"), (t.description, 0.5, "description")]

    def to_item(self, t, score, field):
        return SearchResultItem(
            type=self.type,
            id=t.name,
            title=t.display_name or t.name,
            subtitle=f"tag {t.name}",
            score=score,
            match_field=field,
            href=_qs("/inventory/instances", tag=t.name),
            meta={"tag": t.name},
        )


class LocalIdentityAdapter(ResourceAdapter):
    type, label, min_role = "identity", "Local Identities", Role.ADMIN

    def enabled(self, ctx):
        return super().enabled(ctx) and ctx.settings.auth_mode == "client-cert"

    async def fetch(self, ctx):
        stmt = select(LocalIdentity).order_by(LocalIdentity.created_at.desc()).limit(_DB_FETCH_CAP)
        async with async_session_maker() as db:
            return list((await db.execute(stmt)).scalars().all())

    def fields(self, r):
        return [
            (r.common_name, 1.0, "common_name"),
            (r.display_name, 0.9, "display_name"),
            (r.fingerprint, 0.85, "fingerprint"),
            (r.role, 0.6, "role"),
        ]

    def to_item(self, r, score, field):
        return SearchResultItem(
            type=self.type,
            id=r.fingerprint,
            title=r.display_name or r.common_name,
            subtitle=f"{r.role} · {r.status}",
            score=score,
            match_field=field,
            href=_qs("/admin/local-identities", q=r.common_name),
            meta={"role": r.role, "status": r.status, "fingerprint": r.fingerprint},
        )


class AuditAdapter(ResourceAdapter):
    type, label, min_role = "audit", "Audit Log", Role.ADMIN

    def enabled(self, ctx):
        return super().enabled(ctx) and ctx.settings.audit_db_enabled

    async def fetch(self, ctx):
        like = f"%{ctx.query.strip()}%"
        stmt = (
            select(AuditLogEntry)
            .where(
                or_(
                    AuditLogEntry.action.ilike(like),
                    AuditLogEntry.target_id.ilike(like),
                    AuditLogEntry.actor_email.ilike(like),
                    AuditLogEntry.target_host.ilike(like),
                )
            )
            .order_by(AuditLogEntry.occurred_at.desc())
            .limit(_DB_FETCH_CAP)
        )
        async with async_session_maker() as db:
            return list((await db.execute(stmt)).scalars().all())

    def fields(self, e):
        return [
            (e.action, 1.0, "action"),
            (e.target_id, 0.9, "target_id"),
            (e.actor_email, 0.7, "actor"),
            (e.target_host, 0.6, "host"),
        ]

    def to_item(self, e, score, field):
        target = " ".join(p for p in (e.target_type, e.target_id) if p)
        subtitle = " · ".join(p for p in (target, e.actor_email or "system") if p)
        return SearchResultItem(
            type=self.type,
            id=e.event_id,
            title=e.action,
            subtitle=subtitle,
            score=score,
            match_field=field,
            href=_qs("/admin/audit-log", action=e.action),
            meta={"actor": e.actor_email, "result": e.result, "target": target},
        )


# Registry. Order defines the tie-break / fallback ordering between groups.
# note: Organizations have a DB model but no UI page in the mini deployment, so
# they are intentionally not registered here (a result would be a dead link).
ADAPTERS: List[ResourceAdapter] = [
    InstanceAdapter(),
    ProjectAdapter(),
    NetworkAdapter(),
    StoragePoolAdapter(),
    StorageVolumeAdapter(),
    ImageAdapter(),
    ProfileAdapter(),
    TagAdapter(),
    LocalIdentityAdapter(),
    AuditAdapter(),
]
