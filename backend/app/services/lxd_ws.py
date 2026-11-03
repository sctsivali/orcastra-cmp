"""Backend-to-LXD WebSocket plumbing shared by the console and terminal proxies.

important: everything an interactive session does before its first byte is synchronous (pylxd
over requests, the Vault client), and it used to run inline in the async socket handler. Every
other socket on that worker froze while a console or shell opened, for up to the sum of the
timeouts, long enough for uvicorn's keepalive to drop healthy neighbours. `session_io` is the one
door those calls go through: a bounded pool, a per-cluster bound, the caller's context carried
into the thread (the org-scope filter in connection.get_client reads it).

note: the endpoint and client certificate already sit in the connection manager's memory once a
cluster is connected (it keeps them to reconnect without Vault). The SSL context for the LXD
sockets is built from that copy and cached per certificate digest, so a socket no longer costs a
Vault read and a key-pair parse, and a rotated certificate gets a fresh context on its own.
"""

from __future__ import annotations

import hashlib
import logging
import re
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

from urllib.parse import urlparse

from websockets.asyncio.client import ClientConnection, connect as ws_connect

from pylxd.exceptions import NotFound

from app.core.blocking import run_blocking
from app.services.lxd_ssl import create_lxd_ssl_context

logger = logging.getLogger(__name__)

T = TypeVar("T")

_session_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="interactive_session")
_SESSION_MAX_IN_FLIGHT = 16
_SESSION_MAX_PER_CLUSTER = 6
_SESSION_IO_TIMEOUT_SECONDS = 30.0

# LXD answers WebSocket pings (gorilla's default handler pongs while the daemon reads the
# socket, which it always does on a console or exec stream). Without them a half-open
# connection on an idle channel froze the display for good.
LXD_PING_INTERVAL_SECONDS = 30.0
LXD_PING_TIMEOUT_SECONDS = 30.0



_SECRET = re.compile(r"secret=[^&\s'\"]+")


def redact(text: object) -> str:
    """``text`` with any operation secret blanked, for log lines built from exception text.

    note: websockets renders the whole URL in some errors (InvalidURI), and the URL of an LXD
    operation socket carries the secret that opens it.
    """
    return _SECRET.sub("secret=[redacted]", str(text))


class SessionSetupError(Exception):
    """An interactive session could not start. ``retryable`` says whether trying again may help.

    note: the message reaches the operator (as the socket's close reason), so it names the
    instance state or the cluster, never an operation URL or secret.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


_ssl_lock = threading.Lock()
_ssl_cache: Dict[str, Tuple[str, ssl.SSLContext]] = {}


async def session_io(fn: Callable[..., T], *args: Any, label: str, host: str, **kwargs: Any) -> T:
    """Run one blocking setup step for an interactive session off the event loop."""
    return await run_blocking(
        fn,
        *args,
        timeout=_SESSION_IO_TIMEOUT_SECONDS,
        label=label,
        executor=_session_executor,
        max_in_flight=_SESSION_MAX_IN_FLIGHT,
        concurrency_key=host,
        max_in_flight_per_key=_SESSION_MAX_PER_CLUSTER,
        **kwargs,
    )


def lxd_client(host: str):
    """A connected pylxd client for ``host``, or None. Blocking: call through session_io.

    note: the pooled client first, without the validating ``GET /1.0``. The instance read that
    every session does next is the liveness probe; ``client_for_retry`` rebuilds the client when
    that read finds the pooled one dead.
    """
    from app.services.lxd_service import get_lxd_service

    manager = get_lxd_service().connection_manager
    client = manager.get_cached_client(host)
    if client is not None:
        return client
    return get_lxd_service().get_client_with_reconnect(host)


def client_for_retry(host: str):
    """A freshly validated client after the pooled one failed a request. Blocking."""
    from app.services.lxd_service import get_lxd_service

    return get_lxd_service().get_client_with_reconnect(host)


def read_instance(host: str, project: str, instance_name: str) -> Dict[str, Any]:
    """The instance's LXD record. Blocking. Raises SessionSetupError with an actionable message."""
    from app.core.lxd_path import safe_segment

    params = {"project": project or "default"}
    client = lxd_client(host)
    if client is None:
        raise SessionSetupError(f"Cannot reach cluster {host}", retryable=True)
    # important: pylxd raises NotFound for a 404 before a status check here could see one, and a
    # missing instance is an answer, not a stale client worth rebuilding.
    try:
        response = client.api.instances[safe_segment(instance_name)].get(params=params)
    except NotFound as missing:
        raise SessionSetupError(f"Instance {instance_name} was not found", retryable=False) from missing
    except Exception as first:  # noqa: BLE001 - a pooled client can be stale; rebuild it once
        logger.info("Instance read on %s failed with the pooled client (%s); reconnecting", host, type(first).__name__)
        client = client_for_retry(host)
        if client is None:
            raise SessionSetupError(f"Cannot reach cluster {host}", retryable=True) from first
        try:
            response = client.api.instances[safe_segment(instance_name)].get(params=params)
        except NotFound as missing:
            raise SessionSetupError(f"Instance {instance_name} was not found", retryable=False) from missing
    if response.status_code != 200:
        raise SessionSetupError(f"The cluster answered {response.status_code} for {instance_name}", retryable=True)
    return response.json().get("metadata", {}) or {}


def connection_material(host: str) -> Tuple[str, ssl.SSLContext]:
    """The cluster's WebSocket base (``wss://host:port``) and a client SSL context. Blocking.

    Read from the connection manager's in-memory copy; a worker that has not connected to the
    cluster yet connects first. Falls back to Vault only if neither yields the material.
    """
    from app.services.lxd_service import get_lxd_service

    service = get_lxd_service()
    info = service.connection_manager._connection_info.get(host)
    if not info:
        service.get_client_with_reconnect(host)
        info = service.connection_manager._connection_info.get(host)
    if info and info.get("cert_content") and info.get("key_content") and info.get("endpoint"):
        endpoint = str(info["endpoint"])
        cert_pem, key_pem = str(info["cert_content"]), str(info["key_content"])
    else:
        from app.services.console_service import get_console_service

        manager = get_console_service().cert_manager
        endpoint = str(manager.vault.vault_read(f"{manager.clusters_path}/{host}/metadata").get("endpoint", ""))
        cert = manager.get_cluster_certificate(host)
        cert_pem, key_pem = cert.get("client.crt", ""), cert.get("client.key", "")
    if not endpoint:
        raise ValueError(f"No endpoint is registered for {host}")
    return ws_base(endpoint), _ssl_context(host, cert_pem, key_pem)


def connection_pin(host: str) -> str:
    """The certificate fingerprint this worker holds ``host`` to, or "" when it holds none."""
    from app.services.lxd_service import get_lxd_service

    info = get_lxd_service().connection_manager._connection_info.get(host) or {}
    return str(info.get("server_fingerprint") or "")


def socket_material(host: str) -> Tuple[str, ssl.SSLContext, str]:
    """``connection_material`` plus the pin its sockets are held to. Blocking."""
    base, context = connection_material(host)
    return base, context, connection_pin(host)


def der_matches_pin(der: Optional[bytes], pin: str) -> bool:
    """Whether a peer certificate (DER) is the one ``pin`` names (sha256 hex, colons optional)."""
    return bool(der) and hashlib.sha256(der).hexdigest() == pin.replace(":", "").lower()


CERTIFICATE_MISMATCH = "The cluster presented a certificate that does not match the one enrolled"


def pinned_connection(pin: str, refused: Optional[list] = None) -> type:
    """A websockets `create_connection` class that drops the transport unless the peer is `pin`."""
    refused = refused if refused is not None else []

    class PinnedConnection(ClientConnection):
        # important: asyncio calls this once TLS is up and before websockets writes the upgrade
        # request, which is the line that carries the operation secret. A stranger certificate is
        # dropped here, so not one byte of that request reaches it. The REST adapter has always
        # held the pin (FingerprintHTTPAdapter); these sockets were the gap.
        def connection_made(self, transport: Any) -> None:
            super().connection_made(transport)
            ssl_object = transport.get_extra_info("ssl_object")
            der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
            if not der_matches_pin(der, pin):
                refused.append(True)
                transport.abort()

    return PinnedConnection


def ws_base(endpoint: str) -> str:
    """``https://h:p/`` as ``wss://h:p``: the prefix for ``/1.0/operations/<id>/websocket``."""
    return endpoint.replace("https://", "wss://", 1).replace("http://", "ws://", 1).rstrip("/")


def _ssl_context(host: str, cert_pem: str, key_pem: str) -> ssl.SSLContext:
    digest = hashlib.sha256(f"{cert_pem}\n{key_pem}".encode("utf-8")).hexdigest()
    with _ssl_lock:
        cached = _ssl_cache.get(host)
        if cached and cached[0] == digest:
            return cached[1]
    context = create_lxd_ssl_context({"client.crt": cert_pem, "client.key": key_pem})
    with _ssl_lock:
        _ssl_cache[host] = (digest, context)
    return context


def forget_ssl_context(host: str) -> None:
    """Drop the cached context, so the next socket rebuilds it from current material."""
    with _ssl_lock:
        _ssl_cache.pop(host, None)


async def open_lxd_socket(
    url: str,
    ssl_context: ssl.SSLContext,
    *,
    pin: str,
    timeout: float,
    max_size: Optional[int] = 16 * 1024 * 1024,
) -> Any:
    """One WebSocket to an LXD operation, held to the cluster's certificate pin.

    With no pin held (Vault was down when this worker connected) the socket opens unpinned, the
    rule the REST path follows in ``lxd/pin_enforcement.py``, so a console that works today keeps
    working through an outage. In that state the REST response that handed out the operation
    secret was unpinned too, so refusing here alone would protect nothing. The credentials exec,
    whose stdin is a password, and the file explorer refuse an unpinned cluster before any REST
    call instead.

    note: compression off. SPICE images and PTY bytes either arrive compressed or are tiny, and
    deflate on the event loop only added latency. LXD declines the offer anyway; not sending it
    saves the negotiation.

    note: proxy=None. websockets follows HTTPS_PROXY by default, and a pin is only meaningful on
    the direct TLS connection to the daemon.
    """
    import asyncio

    refused: list = []
    if not pin:
        logger.warning("Opening an unpinned LXD socket to %s: no certificate pin is held", urlparse(url).netloc)
    try:
        return await asyncio.wait_for(
            ws_connect(
                url,
                ssl=ssl_context,
                proxy=None,
                compression=None,
                open_timeout=timeout,
                close_timeout=2,
                ping_interval=LXD_PING_INTERVAL_SECONDS,
                ping_timeout=LXD_PING_TIMEOUT_SECONDS,
                max_size=max_size,
                max_queue=64,
                create_connection=pinned_connection(pin, refused) if pin else None,
            ),
            timeout=timeout + 1,
        )
    except Exception:
        if refused:
            logger.error("Refused an LXD socket to %s: certificate does not match the pin", urlparse(url).netloc)
            raise SessionSetupError(CERTIFICATE_MISMATCH, retryable=False) from None
        raise


def operation_urls(base: str, operation: Dict[str, Any]) -> Tuple[str, str, str]:
    """(operation id, data URL, control URL) from an LXD websocket operation response body."""
    metadata = operation.get("metadata", {}) or {}
    operation_id = str(metadata.get("id", "") or "")
    fds = (metadata.get("metadata", {}) or {}).get("fds", {}) or {}
    socket_base = f"{base}/1.0/operations/{operation_id}/websocket"
    return (
        operation_id,
        f"{socket_base}?secret={fds.get('0', '')}",
        f"{socket_base}?secret={fds.get('control', '')}",
    )
