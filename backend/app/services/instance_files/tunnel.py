"""One authenticated stream into one guest's filesystem, opened per request.

The shape is one connection per request and no pool: uploads, downloads and listings arrive on any
of the workers, the upgrade costs about 16 ms, and a pool would have to answer who may reuse a
connection into whose guest.

important: Running is checked here and nowhere else stands in for it. LXD answers 101 for a stopped
container too, mounting its rootfs and reading it from the host (probed on 5.21.8), which is the
path behind Incus CVE-2026-48749, and for a frozen one. So the record is read first and only status
code 103 proceeds.
"""

import asyncio
import logging
import re
import ssl
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Tuple
from urllib.parse import quote, urlsplit

from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.core.engine_baseline import FloorVerdict, floor_verdict
from app.core.lxd_path import safe_segment

from .errors import (
    GuestFileError,
    agent_unavailable,
    busy,
    cluster_not_pinned,
    cluster_unreachable,
    guest_timeout,
    not_running,
)
from .sftp_client import DEFAULT_OP_TIMEOUT, SftpClient

logger = logging.getLogger(__name__)

STATUS_RUNNING = 103
CONNECT_TIMEOUT_SECONDS = 5.0
UPGRADE_TIMEOUT_SECONDS = 10.0
_MAX_ERROR_BODY_BYTES = 8 * 1024
_STATUS_LINE = re.compile(rb"^HTTP/1\.[01] (\d{3})")
_CONTENT_LENGTH = re.compile(rb"(?im)^content-length:\s*(\d+)\s*$")

# The releases carrying the image-symlink fixes live in core/engine_baseline.py ("instance_files").
_SERVER_CHECK_SECONDS = 600.0
_server_checks: Dict[str, Tuple[float, FloorVerdict]] = {}


async def _running_record(host: str, project: str, instance_name: str) -> Dict[str, Any]:
    from app.services.lxd_ws import SessionSetupError, read_instance, session_io

    try:
        record = await session_io(
            read_instance, host, project, instance_name, label="file_instance_read", host=host
        )
    except SessionSetupError as exc:
        if exc.retryable:
            raise cluster_unreachable() from exc
        raise GuestFileError("instance_not_found", 404, "This instance no longer exists.") from exc
    except BlockingPoolFull as exc:
        raise busy() from exc
    except BlockingTimeout as exc:
        raise cluster_unreachable() from exc
    except Exception as exc:  # noqa: BLE001 - LXD's own failure, whatever it was, is not ours to show
        logger.warning("Reading instance %s on %s failed: %s", instance_name, host, type(exc).__name__)
        raise cluster_unreachable() from exc
    if record.get("status_code") != STATUS_RUNNING:
        raise not_running(str(record.get("status") or ""))
    return record


def server_verdict(server: str, version: str) -> FloorVerdict:
    """Whether this daemon carries the fixes for files reached through a symlink in an image.

    Stopped instances are refused, but a stop can race the upgrade, so a daemon that follows such a
    link from the host must not be reachable through here at all. Anything that does not say Incus
    is judged by the LXD lines, as before.
    """
    return floor_verdict("instance_files", "incus" if server == "incus" else "lxd", version)


def is_supported_server(server: str, version: str) -> bool:
    return server_verdict(server, version).allowed


async def _require_supported_server(host: str) -> None:
    from app.services.lxd_ws import lxd_client, session_io

    cached = _server_checks.get(host)
    now = time.monotonic()
    if cached is None or now - cached[0] > _SERVER_CHECK_SECONDS:

        def read() -> FloorVerdict:
            client = lxd_client(host)
            if client is None:
                raise ValueError("no client")
            environment = (client.api.get().json().get("metadata") or {}).get("environment") or {}
            return server_verdict(str(environment.get("server") or ""), str(environment.get("server_version") or ""))

        try:
            verdict = await session_io(read, label="file_server_version", host=host)
        except BlockingPoolFull as exc:
            raise busy() from exc
        except Exception as exc:  # noqa: BLE001 - not knowing is not a yes
            raise cluster_unreachable() from exc
        cached = (now, verdict)
        _server_checks[host] = cached
    verdict = cached[1]
    if not verdict.allowed:
        running = f"{verdict.engine} {verdict.seen}" if verdict.seen else "a version"
        upgrade = f" Upgrade to {verdict.minimum} or later." if verdict.minimum else ""
        raise GuestFileError(
            "unsupported_server", 503, f"This cluster runs {running}, too old for safe file access.{upgrade}"
        )


async def _material(host: str) -> Tuple[str, ssl.SSLContext, str]:
    from app.services.lxd_ws import session_io, socket_material

    try:
        base, context, pin = await session_io(socket_material, host, label="file_tunnel_material", host=host)
    except BlockingPoolFull as exc:
        raise busy() from exc
    except (BlockingTimeout, ValueError) as exc:
        raise cluster_unreachable() from exc
    if not pin:
        # note: REST carries on unpinned through a Vault outage (lxd/pin_enforcement.py). A guest's
        # files are not worth that risk, so this path refuses until a pin is held.
        raise cluster_not_pinned()
    return base, context, pin


def _matches_pin(writer: asyncio.StreamWriter, pin: str) -> bool:
    from app.services.lxd_ws import der_matches_pin

    ssl_object = writer.get_extra_info("ssl_object")
    return der_matches_pin(ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None, pin)


async def _refused(reader: asyncio.StreamReader, head: bytes, code: int, record: Dict[str, Any]) -> GuestFileError:
    if code == 404:
        return GuestFileError("instance_not_found", 404, "This instance no longer exists.")
    if code in (400, 501):
        return GuestFileError("unsupported_server", 503, "This cluster does not offer file access.")
    if code == 403:
        return GuestFileError("cluster_refused", 502, "The cluster refused this service's certificate.")
    declared = _CONTENT_LENGTH.search(head)
    body = b""
    if declared:
        try:
            async with asyncio.timeout(2):
                body = await reader.read(min(int(declared.group(1)), _MAX_ERROR_BODY_BYTES))
        except TimeoutError:
            pass
    if record.get("type") == "virtual-machine" or b"agent" in body.lower():
        return agent_unavailable()
    return GuestFileError("guest_io_error", 502, "The cluster could not open this instance's files.")


async def _upgrade(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    netloc: str,
    project: str,
    instance_name: str,
    record: Dict[str, Any],
) -> None:
    request = (
        f"GET /1.0/instances/{safe_segment(instance_name)}/sftp?project={quote(project, safe='')} HTTP/1.1\r\n"
        f"Host: {netloc}\r\n"
        "Connection: Upgrade\r\n"
        "Upgrade: sftp\r\n"
        "User-Agent: orcastra-dashboard\r\n"
        "\r\n"
    )
    writer.write(request.encode("ascii"))
    try:
        async with asyncio.timeout(UPGRADE_TIMEOUT_SECONDS):
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
    except TimeoutError:
        raise guest_timeout() from None
    except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, ConnectionError) as exc:
        raise cluster_unreachable() from exc
    status_line = _STATUS_LINE.match(head)
    code = int(status_line.group(1)) if status_line else 0
    if code != 101:
        raise await _refused(reader, head, code, record)


@asynccontextmanager
async def open_guest(
    host: str,
    project: str,
    instance_name: str,
    *,
    op_timeout: float = DEFAULT_OP_TIMEOUT,
) -> AsyncIterator[SftpClient]:
    """An SFTP client into a Running instance. Leaving the block ends the connection."""
    await _require_supported_server(host)
    record = await _running_record(host, project, instance_name)
    base, context, pin = await _material(host)
    target = urlsplit(base)
    if not target.hostname:
        raise cluster_unreachable()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                target.hostname,
                target.port or 8443,
                ssl=context,
                ssl_handshake_timeout=CONNECT_TIMEOUT_SECONDS,
            ),
            CONNECT_TIMEOUT_SECONDS + 1,
        )
    except (OSError, TimeoutError, ssl.SSLError) as exc:
        logger.info("File tunnel to %s did not connect: %s", host, type(exc).__name__)
        raise cluster_unreachable() from exc
    try:
        # important: before a byte of HTTP on this socket. The client certificate has been shown by
        # now, which is public, and nothing that opens a guest leaves until the peer is proved.
        if not _matches_pin(writer, pin):
            logger.warning("File tunnel to %s refused: certificate does not match the pin", host)
            raise cluster_not_pinned()
        await _upgrade(reader, writer, target.netloc, project, instance_name, record)
        # note: LXD has started its side by now, so this narrows a stop racing the upgrade rather
        # than closing it, and the supported-version floor is what makes that race harmless.
        await _running_record(host, project, instance_name)
        async with SftpClient(reader, writer, op_timeout=op_timeout) as client:
            yield client
    finally:
        writer.transport.abort()
