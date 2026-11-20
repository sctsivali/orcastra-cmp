"""Host-side recovery for Mini, run inside the backend container by someone with root on the host.

    docker exec -it <prefix>-backend python -m app.editions.mini.cli recover-admin --email you@example.org

Issues a new Administrator certificate when no Administrator can sign in (every Administrator
certificate lost, revoked or expired). The .p12 is written to /app/config (the deployment's config
directory on the host) and its password printed once on this terminal. Shell access to the host is
the authority here, the same authority that can read the database and Vault keys.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import func, select


async def recover_admin(email: str, out_dir: str, force: bool) -> int:
    from app.core.database import async_session_maker
    from app.editions.mini import identities
    from app.editions.mini.audit import security_event
    from app.editions.mini.issuing import issue_bundle
    from app.models.mini_identity_models import LocalIdentity

    if await identities.active_admin_exists() and not force:
        print("An Administrator can still sign in; ask them to issue a certificate instead "
              "(or pass --force to issue one anyway).", file=sys.stderr)
        return 2
    email = email.strip().lower()
    bundle = await issue_bundle(email)
    async with async_session_maker() as session:
        identity = (await session.execute(
            select(LocalIdentity).where(func.lower(LocalIdentity.email) == email)
        )).scalar_one_or_none()
        if identity is None:
            snapshot = await identities.create_identity(
                email=email, common_name=bundle.certificate.common_name, display_name=None, role="admin",
                created_by="recovery", issued_by="host-recovery", owner_org_id=None,
                fingerprint=bundle.certificate.fingerprint, serial=bundle.certificate.serial,
                not_after=bundle.certificate.not_after, cert_common_name=bundle.certificate.common_name, session=session,
            )
            target = snapshot.uuid
        else:
            identity.role, identity.status = "admin", "active"
            identity.tokens_valid_after = datetime.now(timezone.utc)
            await identities.add_certificate(identity.id, fingerprint=bundle.certificate.fingerprint,
                                             serial=bundle.certificate.serial, not_after=bundle.certificate.not_after,
                                             cert_common_name=bundle.certificate.common_name, renewed_from_id=None,
                                             session=session)
            target = identity.uuid
        await security_event("identity.recover_admin", target_type="identity", target_id=target, severity="critical",
                             actor_user_id="host-recovery",
                             details={"email": email, "fingerprint": bundle.certificate.fingerprint}, session=session)
        await session.commit()
    path = os.path.join(out_dir, f"recovered-admin-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.p12")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(bundle.pfx)
    print(f"Administrator certificate for {email} written to {path} (on the host: the deployment's config directory).")
    print(f"Import password (shown once): {bundle.password}")
    print("Import it into your browser, sign in, then delete the file.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.editions.mini.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("recover-admin", help="issue an Administrator certificate when no Administrator can sign in")
    rec.add_argument("--email", required=True)
    rec.add_argument("--out-dir", default="/app/config")
    rec.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "recover-admin":
        return asyncio.run(recover_admin(args.email, args.out_dir, args.force))
    return 1


if __name__ == "__main__":
    sys.exit(main())
