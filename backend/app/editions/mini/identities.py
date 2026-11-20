"""Operator identities and their certificates: lookups for every request, and the changes to them.

Every authenticated request reads the caller's identity: whether it is active, its role, the
earliest token it still accepts, and the state of the certificate the request presented. The read
goes to Postgres, with a two-second per-worker memo so a page that fires thirty calls at once costs
one query. A change made through this module drops the memo in its own worker at once; another
worker sees it within two seconds. A database that cannot answer refuses the request.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
import uuid as uuid_module
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.database import async_session_maker
from app.models.mini_identity_models import LocalIdentity, LocalIdentityCertificate

MEMO_SECONDS = 2.0
ROLES = ("admin", "partner", "tenant")
_LABEL = re.compile(r"[^a-z0-9-]+")


@dataclass(frozen=True)
class CertificateState:
    id: int
    status: str
    not_after: Optional[datetime]
    renewed_from_id: Optional[int]


@dataclass(frozen=True)
class IdentitySnapshot:
    id: int
    uuid: str
    email: str
    display_name: Optional[str]
    common_name: str
    role: str
    status: str
    owner_org_id: Optional[int]
    tokens_valid_after: Optional[datetime]
    certificates: Dict[str, CertificateState] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.status == "active"

    def token_floor(self) -> int:
        """The earliest `iat` (whole seconds) a session token of this identity may carry."""
        if self.tokens_valid_after is None:
            return 0
        return int(self.tokens_valid_after.timestamp())

    def certificate_usable(self, fingerprint: str, when: datetime) -> bool:
        cert = self.certificates.get(fingerprint)
        if cert is None or cert.status != "active":
            return False
        return cert.not_after is None or when <= cert.not_after


_memo: Dict[str, Tuple[float, Optional[IdentitySnapshot]]] = {}


def forget(uuid: Optional[str] = None) -> None:
    if uuid is None:
        _memo.clear()
    else:
        _memo.pop(uuid, None)


def _snapshot(identity: LocalIdentity, certificates) -> IdentitySnapshot:
    return IdentitySnapshot(
        id=identity.id, uuid=identity.uuid, email=identity.email, display_name=identity.display_name,
        common_name=identity.common_name, role=identity.role, status=identity.status,
        owner_org_id=identity.owner_org_id, tokens_valid_after=identity.tokens_valid_after,
        certificates={
            c.fingerprint: CertificateState(c.id, c.status, c.not_after, c.renewed_from_id) for c in certificates
        },
    )


async def _load(session, where) -> Optional[IdentitySnapshot]:
    identity = (await session.execute(select(LocalIdentity).where(where))).scalar_one_or_none()
    if identity is None:
        return None
    certificates = (await session.execute(
        select(LocalIdentityCertificate).where(LocalIdentityCertificate.identity_id == identity.id)
    )).scalars().all()
    return _snapshot(identity, certificates)


async def snapshot_by_uuid(uuid: str) -> Optional[IdentitySnapshot]:
    now = time.monotonic()
    memo = _memo.get(uuid)
    if memo is not None and now - memo[0] < MEMO_SECONDS:
        return memo[1]
    async with async_session_maker() as session:
        snap = await _load(session, LocalIdentity.uuid == uuid)
    _memo[uuid] = (now, snap)
    return snap


async def snapshot_by_email(email: str) -> Optional[IdentitySnapshot]:
    async with async_session_maker() as session:
        return await _load(session, func.lower(LocalIdentity.email) == (email or "").strip().lower())


async def snapshot_by_certificate(fingerprint: str) -> Optional[IdentitySnapshot]:
    async with async_session_maker() as session:
        identity_id = (await session.execute(
            select(LocalIdentityCertificate.identity_id).where(LocalIdentityCertificate.fingerprint == fingerprint.lower())
        )).scalar_one_or_none()
        if identity_id is None:
            return None
        return await _load(session, LocalIdentity.id == identity_id)


async def active_admin_exists() -> bool:
    """An active, unexpired Administrator certificate exists; the bootstrap token works only without one."""
    now = datetime.now(timezone.utc)
    async with async_session_maker() as session:
        found = (await session.execute(
            select(func.count()).select_from(LocalIdentity).join(
                LocalIdentityCertificate, LocalIdentityCertificate.identity_id == LocalIdentity.id
            ).where(
                LocalIdentity.role == "admin", LocalIdentity.status == "active",
                LocalIdentityCertificate.status == "active",
                (LocalIdentityCertificate.not_after.is_(None)) | (LocalIdentityCertificate.not_after > now),
            )
        )).scalar_one()
    return found > 0


def default_email(common_name: str, suffix: str) -> str:
    label = _LABEL.sub("-", (common_name or "").split(".", 1)[0].lower()).strip("-") or "operator"
    return f"{label[:64]}@{suffix}"


def new_password() -> str:
    """A .p12 import password: shown once, typed by a person, 20 characters."""
    return secrets.token_urlsafe(15)


class IdentityConflict(Exception):
    """The email or certificate already belongs to an identity (a tombstoned one included)."""


async def create_identity(*, email: str, common_name: str, display_name: Optional[str], role: str,
                          created_by: str, issued_by: Optional[str], owner_org_id: Optional[int],
                          fingerprint: str, serial: Optional[str], not_after: Optional[datetime],
                          cert_common_name: str, session=None) -> IdentitySnapshot:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")

    async def _do(s):
        identity = LocalIdentity(
            uuid=str(uuid_module.uuid4()), email=email.strip().lower(), common_name=common_name,
            display_name=display_name, role=role, status="active", owner_org_id=owner_org_id,
            issued_by=issued_by, created_by=created_by,
        )
        s.add(identity)
        await s.flush()
        cert = LocalIdentityCertificate(
            identity_id=identity.id, fingerprint=fingerprint.lower(), common_name=cert_common_name,
            serial=serial, not_after=not_after, status="active",
        )
        s.add(cert)
        await s.flush()
        return _snapshot(identity, [cert])

    try:
        if session is not None:
            return await _do(session)
        async with async_session_maker() as s:
            snap = await _do(s)
            await s.commit()
            return snap
    except IntegrityError as exc:
        raise IdentityConflict(str(exc.orig) if exc.orig else "conflict") from exc


async def add_certificate(identity_id: int, *, fingerprint: str, serial: Optional[str],
                          not_after: Optional[datetime], cert_common_name: str,
                          renewed_from_id: Optional[int], session) -> CertificateState:
    cert = LocalIdentityCertificate(
        identity_id=identity_id, fingerprint=fingerprint.lower(), common_name=cert_common_name,
        serial=serial, not_after=not_after, status="active", renewed_from_id=renewed_from_id,
    )
    session.add(cert)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise IdentityConflict("certificate already enrolled") from exc
    return CertificateState(cert.id, cert.status, cert.not_after, cert.renewed_from_id)


async def record_login(snapshot: IdentitySnapshot, fingerprint: str) -> None:
    """Stamp the login; the first use of a renewed certificate retires the one it replaced."""
    now = datetime.now(timezone.utc)
    cert = snapshot.certificates.get(fingerprint)
    async with async_session_maker() as s:
        await s.execute(update(LocalIdentity).where(LocalIdentity.id == snapshot.id).values(last_login_at=now))
        if cert is not None:
            await s.execute(update(LocalIdentityCertificate).where(
                LocalIdentityCertificate.id == cert.id).values(last_used_at=now))
            if cert.renewed_from_id is not None:
                await s.execute(update(LocalIdentityCertificate).where(
                    LocalIdentityCertificate.id == cert.renewed_from_id,
                    LocalIdentityCertificate.status == "active",
                ).values(status="superseded", revoked_at=now))
        await s.commit()
    forget(snapshot.uuid)


async def retire_overlapping(overlap_hours: int) -> int:
    """Supersede certificates a renewal replaced once the overlap has passed unused."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=overlap_hours)
    async with async_session_maker() as s:
        renewed = select(LocalIdentityCertificate.renewed_from_id).where(
            LocalIdentityCertificate.renewed_from_id.is_not(None),
            LocalIdentityCertificate.created_at < cutoff,
        )
        result = await s.execute(update(LocalIdentityCertificate).where(
            LocalIdentityCertificate.id.in_(renewed), LocalIdentityCertificate.status == "active",
        ).values(status="superseded", revoked_at=datetime.now(timezone.utc)))
        await s.commit()
    forget()
    return result.rowcount or 0


async def _blocking(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)
