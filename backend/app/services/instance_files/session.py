"""The steps every file request takes before it may touch a guest.

The caller has already passed `verify_file_access`, which each handler calls itself so that the
cluster-scope ratchet can see it. This adds what the shell's gate adds: the break-glass rule, the
migration fence, this worker's limits, and the turning of a guest failure into an HTTP answer.

important: no `guard_instance_mutation` here. That guard ends in a per-cluster
`pg_advisory_xact_lock`, so a guest stalling one reply would queue every mutation on its cluster
behind it, and each waiter would pin a database connection. The fence is read without the lock
instead, before a body and again before a commit. What remains is the window an open shell already
has, which is recorded in documentation/adr/ and acceptable because the only thing racing is a
guest's own filesystem.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from app.core.auth import AuthenticatedUser
from app.core.database import async_session_maker
from app.services.break_glass_service import enforce_sensitive_access
from app.services.migration_fence import MigrationFenceConflict, assert_instance_not_migration_fenced

from .audit import write
from .errors import GuestFileError, guest_timeout
from .limits import short_slot, transfer_slot
from .sftp_client import DEFAULT_OP_TIMEOUT, SftpClient
from .tunnel import open_guest

logger = logging.getLogger(__name__)

# Under the proxy's 100 s deadline, so a request that will not finish answers here rather than as
# the proxy's 502. Nothing else ends a handler a guest keeps busy once its client has left.
REQUEST_SECONDS = 90.0


def user_key(user: Optional[AuthenticatedUser]) -> str:
    return user.user_id if user is not None else "anonymous"


async def sensitive_gate(user: Optional[AuthenticatedUser], host: str) -> Optional[int]:
    """The live break-glass grant this access runs under, or None when the caller owns the cluster."""
    if user is None:
        return None
    # note: its own session, closed at once. Handlers release the request's session before a guest
    # round trip, so a stalled guest cannot hold a pooled connection.
    async with async_session_maker() as db:
        grant = await enforce_sensitive_access(user, host, db)
    return grant.id if grant is not None else None


async def fence_gate(host: str, project: str, instance: str) -> None:
    try:
        await assert_instance_not_migration_fenced(cluster_id=host, project=project, instance_name=instance)
    except MigrationFenceConflict as exc:
        raise GuestFileError(
            "migration_fenced", 409, "This instance is being migrated. Files are back when it finishes."
        ) from exc


@asynccontextmanager
async def http_failures() -> AsyncIterator[None]:
    """Guest failures as HTTP answers. A browser that left needs no answer at all."""
    try:
        yield
    except GuestFileError as failure:
        if failure.code == "guest_protocol_error":
            logger.warning("Refused a guest reply: %s", failure.extra.get("reason"))
            failure.extra.pop("reason", None)
        raise failure.to_http() from None
    except TimeoutError:
        # This request's own wall clock, not one operation's.
        raise guest_timeout().to_http() from None
    except ClientDisconnect:
        raise HTTPException(status_code=499, detail="The client went away") from None


@asynccontextmanager
async def guest_session(
    user: Optional[AuthenticatedUser],
    host: str,
    project: str,
    instance: str,
    *,
    transfer: bool = False,
    record: Optional[Dict[str, Any]] = None,
    op_timeout: float = DEFAULT_OP_TIMEOUT,
    route: str = "",
) -> AsyncIterator[SftpClient]:
    """Admit, take a slot, open the guest. Leaving the block ends the connection.

    `route` names a read that has no record of its own. Under a break-glass grant it gets one,
    because what an Administrator did inside a tenant's guest is what that grant promises to show.
    """
    async with http_failures():
        grant_id = await sensitive_gate(user, host)
        if record is not None:
            record["break_glass_grant_id"] = grant_id
        elif grant_id is not None:
            await write(
                "file.browse.granted", host=host, project=project, instance=instance, result="success",
                details={"route": route, "break_glass_grant_id": grant_id},
            )
        await fence_gate(host, project, instance)
        slot = (
            transfer_slot(host, project, instance, user_key(user))
            if transfer
            else short_slot(host, project, instance, user_key(user))
        )
        async with asyncio.timeout(REQUEST_SECONDS), slot:
            async with open_guest(host, project, instance, op_timeout=op_timeout) as guest:
                yield guest
