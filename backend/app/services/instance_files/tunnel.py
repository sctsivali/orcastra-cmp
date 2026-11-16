"""One authenticated stream into one guest's filesystem, opened per request.

One connection per request and no pool: uploads, downloads and listings arrive on any worker, the
upgrade costs about 16 ms, and a pool would have to decide who may reuse a connection into whose
guest.

important: Running is checked here and nothing else stands in for it. LXD answers 101 for a
stopped container too, mounting its rootfs and reading it from the host (LXD 5.21.8; the path
behind Incus CVE-2026-48749), and for a frozen one. So the record is read first and only status
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
from app.services.lxd_certs.pinning import endpoint_host_port

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


def _unreachable(host: str, step: str, cause: object = None) -> GuestFileError:
    """`cluster_unreachable`, with the step and the real cause left in the log.

    The answer to the browser stays the same generic code. An operator reading the log needs the
    step, the socket and the error text, or every one of these exits looks like a dead cluster.
    """
    from app.services.lxd_ws import redact

    detail = f"{type(cause).__name__}: {redact(cause)}" if isinstance(cause, BaseException) else str(cause or "")
    logger.warning("File access to %s could not reach the cluster at %s: %s", host, step, detail)
    return cluster_unreachable()


async def _running_record(host: str, project: str, instance_name: str) -> Dict[str, Any]:
    from app.services.lxd_ws import SessionSetupError, read_instance, session_io

    try:
        record = await session_io(
            read_instance, host, project, instance_name, label="file_instance_read", host=host
        )
    except SessionSetupError as exc:
        if exc.retryable:
            raise _unreachable(host, "instance read", exc) from exc
        raise GuestFileError("instance_not_found", 404, "This instance no longer exists.") from exc
    except BlockingPoolFull as exc:
        raise busy() from exc
    except BlockingTimeout as exc:
        raise _unreachable(host, "instance read (worker queue)", exc) from exc
    except Exception as exc:  # noqa: BLE001 - LXD's failure text is not shown to the caller
        raise _unreachable(host, f"instance read of {instance_name}", exc) from exc
    if record.get("status_code") != STATUS_RUNNING:
        raise not_running(str(record.get("status") or ""))
    return record


def server_verdict(server: str, version: str) -> FloorVerdict:
    """Whether this daemon carries the fixes for files reached through a symlink in an image.

    Stopped instances are refused, but a stop can race the upgrade, so a daemon that follows such a
    link from the host must not be reachable through here at all. Any server other than Incus is
    judged by the LXD release lines.
    """
    return floor_verdict("instance_files", "incus" if server == "incus" else "lxd", version)


def is_supported_server(server: str, version: str) -> bool:
    return server_verdict(server, version).allowed


class _NoClient(Exception):
    """This worker holds no connected client: the breaker is open or the last connect failed."""


def _server_environment(host: str) -> Dict[str, Any]:
    """`GET /1.0`'s environment. Blocking. A stale pooled client is rebuilt once, as the instance
    read does (lxd_ws.read_instance), so a connection the cluster closed is not called an outage."""
    from app.services import lxd_ws

    client = lxd_ws.lxd_client(host)
    if client is None:
        client = lxd_ws.client_for_retry(host)
        if client is None:
            raise _NoClient("no connected client (breaker open or connect failed)")
        return (client.api.get().json().get("metadata") or {}).get("environment") or {}
    try:
        return (client.api.get().json().get("metadata") or {}).get("environment") or {}
    except Exception as first:  # noqa: BLE001 - a pooled client can be stale; rebuild it once
        client = lxd_ws.client_for_retry(host)
        if client is None:
            raise _NoClient("no connected client (breaker open or connect failed)") from first
        return (client.api.get().json().get("metadata") or {}).get("environment") or {}


async def _require_supported_server(host: str) -> None:
    from app.services import lxd_ws

    cached = _server_checks.get(host)
    now = time.monotonic()
    if cached is None or now - cached[0] > _SERVER_CHECK_SECONDS:

        def read() -> FloorVerdict:
            environment = _server_environment(host)
            return server_verdict(str(environment.get("server") or ""), str(environment.get("server_version") or ""))

        try:
            verdict = await lxd_ws.session_io(read, label="file_server_version", host=host)
        except BlockingPoolFull as exc:
            raise busy() from exc
        except Exception as exc:  # noqa: BLE001 - an unknown version is refused
            raise _unreachable(host, "version check", exc) from exc
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
        raise _unreachable(host, "connection material", exc) from exc
    if not pin:
        # REST continues unpinned through a Vault outage (lxd/pin_enforcement.py). A guest's files
        # are not worth that risk, so this path refuses until a pin is held.
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
    # A VM's files go through lxd-agent, and LXD's refusal names it. An empty or unread body on a VM
    # still reads as the agent; a body that says something else (a proxy's 502 page, another LXD
    # error) is not blamed on the guest.
    if b"agent" in body.lower() or (record.get("type") == "virtual-machine" and not body.strip()):
        return agent_unavailable()
    logger.warning("File tunnel upgrade refused with HTTP %s: %r", code, body[:200])
    return GuestFileError("guest_io_error", 502, "The cluster could not open this instance's files.")


async def _upgrade(
    host: str,
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
        raise _unreachable(host, "upgrade reply", exc) from exc
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
    try:
        # important: the same socket the REST client, the terminal and the pin probe use. A
        # portless endpoint is served on the scheme's port (443), never LXD's 8443.
        dial_host, dial_port = endpoint_host_port(base)
    except ValueError as exc:
        raise _unreachable(host, "endpoint", exc) from exc
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                dial_host,
                dial_port,
                ssl=context,
                ssl_handshake_timeout=CONNECT_TIMEOUT_SECONDS,
            ),
            CONNECT_TIMEOUT_SECONDS + 1,
        )
    except (OSError, TimeoutError, ssl.SSLError) as exc:
        raise _unreachable(host, f"connect to {dial_host}:{dial_port}", exc) from exc
    try:
        # important: check the pin before any HTTP on this socket. Only the (public) client
        # certificate has been sent so far; nothing that opens a guest goes out until the peer
        # is proved.
        if not _matches_pin(writer, pin):
            logger.warning("File tunnel to %s refused: certificate does not match the pin", host)
            raise cluster_not_pinned()
        await _upgrade(host, reader, writer, urlsplit(base).netloc, project, instance_name, record)
        # LXD has started its side by now, so this only narrows a stop racing the upgrade; the
        # supported-version floor makes that race harmless.
        await _running_record(host, project, instance_name)
        async with SftpClient(reader, writer, op_timeout=op_timeout) as client:
            yield client
    finally:
        writer.transport.abort()
