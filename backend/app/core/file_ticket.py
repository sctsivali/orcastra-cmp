"""Single-use tickets that let a browser's own download manager fetch a guest file.

A browser cannot attach a Bearer header to a navigation, and fetching into a blob holds the whole
file in memory, so a multi-gigabyte download needs a credential in its URL. A ticket is minted over
an authenticated POST, lives 30 seconds, names exact paths on one instance, and is deleted when
redeemed. It is a sibling of the WebSocket ticket rather than a new kind of one: its own store,
prefix and MAC context mean neither ticket can be spent on the other's door. The URL is harmless
in a log, since `ticket` is a redacted parameter and the value is already spent by then.

note: no Range. A resumed download would present a spent ticket, so a retry mints a new one.
"""

import json
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.core.auth import AuthenticatedUser
from app.core.ws_ticket import TICKET_TTL_SECONDS, TicketClaims, WsTarget, claims_for
from app.core.ws_ticket_store import WsTicketStore

KIND = "instance_file_download"
MAX_TICKET_BYTES = 256 * 1024
_BUDGET_PER_MINUTE = 120
_CLOCK_SKEW_SECONDS = 5.0


class TicketTooLarge(Exception):
    """This selection would not fit a ticket."""


@dataclass(frozen=True)
class FileDownload:
    identity: TicketClaims
    paths: Tuple[str, ...]
    archive: bool
    name: str
    size: Optional[int]
    op_id: str

    @property
    def host(self) -> str:
        return str(self.identity.target.host)

    @property
    def project(self) -> str:
        return str(self.identity.target.project)

    @property
    def instance(self) -> str:
        return str(self.identity.target.instance)


def _encode(download: FileDownload) -> str:
    return json.dumps(
        {
            "identity": download.identity.to_json(),
            "paths": list(download.paths),
            "archive": download.archive,
            "name": download.name,
            "size": download.size,
            "op_id": download.op_id,
        },
        separators=(",", ":"),
    )


def _decode(raw: str) -> FileDownload:
    body = json.loads(raw)
    size = body.get("size")
    return FileDownload(
        identity=TicketClaims.from_json(body["identity"]),
        paths=tuple(str(path) for path in body["paths"]),
        archive=bool(body["archive"]),
        name=str(body["name"]),
        size=int(size) if size is not None else None,
        op_id=str(body["op_id"]),
    )


_store: Optional[WsTicketStore] = None


def get_file_ticket_store() -> WsTicketStore:
    global _store
    if _store is None:
        _store = WsTicketStore(
            ticket_prefix="orcastra:file_ticket",
            budget_prefix="orcastra:file_ticket_budget",
            mac_context=b"orcastra:file-ticket:v1",
            budget=_BUDGET_PER_MINUTE,
        )
    return _store


def set_file_ticket_store(store: Optional[WsTicketStore]) -> None:
    """Replace the process's store. For tests."""
    global _store
    _store = store


async def mint(
    user: Optional[AuthenticatedUser],
    *,
    host: str,
    project: str,
    instance: str,
    paths: List[str],
    archive: bool,
    name: str,
    size: Optional[int],
    op_id: str,
) -> str:
    identity = claims_for(KIND, WsTarget(host=host, project=project, instance=instance), user)
    download = FileDownload(identity, tuple(paths), archive, name, size, op_id)
    encoded = _encode(download)
    # important: this store is the Redis every tenant's cache and socket tickets live in, under
    # allkeys-lru. A private code point costs 12 bytes of JSON, so a modest request could stand for
    # tens of megabytes and evict everyone else's keys.
    if len(encoded) > MAX_TICKET_BYTES:
        raise TicketTooLarge()
    return await get_file_ticket_store().mint(identity.user_id, encoded)


async def redeem(ticket: str) -> Optional[FileDownload]:
    """The download a ticket allows, once. None for anything else, whatever the reason."""
    raw = await get_file_ticket_store().redeem(ticket)
    if raw is None:
        return None
    try:
        download = _decode(raw)
    except (KeyError, TypeError, ValueError):
        return None
    claims = download.identity
    if claims.kind != KIND:
        return None
    if claims.minted_at + TICKET_TTL_SECONDS + _CLOCK_SKEW_SECONDS < time.time():
        return None
    if claims.session_exp is not None and claims.session_exp <= time.time():
        return None
    return download
