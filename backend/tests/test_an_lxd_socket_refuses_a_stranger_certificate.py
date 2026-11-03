"""Every LXD operation socket is held to the cluster's certificate pin before it says anything.

The upgrade request of an operation socket carries the secret that opens it, and on a terminal or
a credentials exec what follows is a root shell or a password. These tests stand up a real TLS
server on loopback and prove that a stranger certificate receives not one byte, that the enrolled
one connects, and that a worker holding no pin still connects (the REST rule during a Vault outage).
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import ssl
from pathlib import Path
from typing import List, Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.asyncio.server import serve

from app.services import lxd_ws
from app.services.lxd_ws import CERTIFICATE_MISMATCH, SessionSetupError, der_matches_pin, open_lxd_socket


def _certificate(tmp: Path, name: str) -> Tuple[ssl.SSLContext, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp / f"{name}.crt", tmp / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def _client_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _colons(hexdigest: str) -> str:
    return ":".join(hexdigest[i:i + 2] for i in range(0, len(hexdigest), 2)).upper()


def test_a_pin_matches_with_or_without_colons_and_never_matches_nothing():
    der = b"certificate bytes"
    digest = hashlib.sha256(der).hexdigest()
    assert der_matches_pin(der, digest)
    assert der_matches_pin(der, _colons(digest))
    assert not der_matches_pin(der, "00" * 32)
    assert not der_matches_pin(None, digest)
    assert not der_matches_pin(b"", digest)


class _Recorder(asyncio.Protocol):
    """A TLS server that answers nothing and keeps every application byte it is sent."""

    def __init__(self, sink: List[bytes]) -> None:
        self.sink = sink

    def data_received(self, data: bytes) -> None:
        self.sink.append(data)


async def _record(server_context: ssl.SSLContext, pin: str) -> Tuple[bytes, BaseException]:
    sink: List[bytes] = []
    loop = asyncio.get_running_loop()
    server = await loop.create_server(lambda: _Recorder(sink), "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]
    url = f"wss://127.0.0.1:{port}/1.0/operations/op/websocket?secret=fd0-secret"
    try:
        await open_lxd_socket(url, _client_context(), pin=pin, timeout=1)
    except BaseException as exc:  # noqa: BLE001 - both cases end in a refusal or a timeout
        failure: BaseException = exc
    else:
        raise AssertionError("a silent server cannot complete a WebSocket handshake")
    await asyncio.sleep(0.2)
    server.close()
    await server.wait_closed()
    return b"".join(sink), failure


def test_a_stranger_certificate_receives_not_one_byte(tmp_path):
    server_context, _ = _certificate(tmp_path, "stranger")
    _, enrolled_pin = _certificate(tmp_path, "enrolled")

    received, failure = asyncio.run(_record(server_context, enrolled_pin))

    assert isinstance(failure, SessionSetupError)
    assert str(failure) == CERTIFICATE_MISMATCH
    assert failure.retryable is False
    assert received == b"", "the stranger saw bytes of the upgrade request"


def test_the_recorder_does_see_the_request_when_the_pin_matches(tmp_path):
    # The control for the test above: the same silent server, the right pin. Without it, "no bytes"
    # could mean the recorder never saw anything at all.
    server_context, pin = _certificate(tmp_path, "enrolled")

    received, failure = asyncio.run(_record(server_context, pin))

    assert not isinstance(failure, SessionSetupError)
    assert received.startswith(b"GET /1.0/operations/op/websocket?secret=fd0-secret HTTP/1.1")


def test_the_enrolled_certificate_connects_and_the_secret_arrives(tmp_path):
    server_context, pin = _certificate(tmp_path, "enrolled")
    paths: List[str] = []

    async def run() -> None:
        async def handler(connection) -> None:
            paths.append(connection.request.path)
            await connection.send(b"ready")

        async with serve(handler, "127.0.0.1", 0, ssl=server_context) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"wss://127.0.0.1:{port}/1.0/operations/op/websocket?secret=fd0-secret"
            socket = await open_lxd_socket(url, _client_context(), pin=_colons(pin), timeout=5)
            assert await socket.recv() == b"ready"
            await socket.close()

    asyncio.run(run())
    assert paths == ["/1.0/operations/op/websocket?secret=fd0-secret"]


def test_without_a_pin_the_socket_opens_and_says_so(tmp_path, caplog):
    server_context, _ = _certificate(tmp_path, "any")

    async def run() -> None:
        async def handler(connection) -> None:
            await connection.send(b"ready")

        async with serve(handler, "127.0.0.1", 0, ssl=server_context) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"wss://127.0.0.1:{port}/1.0/operations/op/websocket?secret=fd0-secret"
            socket = await open_lxd_socket(url, _client_context(), pin="", timeout=5)
            assert await socket.recv() == b"ready"
            await socket.close()

    with caplog.at_level("WARNING", logger=lxd_ws.logger.name):
        asyncio.run(run())
    warnings = [r.getMessage() for r in caplog.records if r.name == lxd_ws.logger.name]
    assert any("unpinned" in message for message in warnings)
    assert not any("fd0-secret" in message for message in warnings), "a log line carried the secret"


def test_an_https_proxy_in_the_environment_is_not_followed(tmp_path, monkeypatch):
    server_context, pin = _certificate(tmp_path, "enrolled")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")

    async def run() -> None:
        async def handler(connection) -> None:
            await connection.send(b"direct")

        async with serve(handler, "127.0.0.1", 0, ssl=server_context) as server:
            port = server.sockets[0].getsockname()[1]
            socket = await open_lxd_socket(f"wss://127.0.0.1:{port}/x?secret=s", _client_context(), pin=pin, timeout=5)
            assert await socket.recv() == b"direct"
            await socket.close()

    asyncio.run(run())


def test_the_events_socket_is_pinned_and_never_proxied():
    import ast
    from pathlib import Path

    source = (Path(lxd_ws.__file__).parent / "lxd" / "events_hub.py").read_text()
    calls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call) and ast.unparse(n.func) == "websockets.connect"]
    assert calls, "the events hub no longer opens its socket where this test looks"
    for call in calls:
        keywords = {k.arg: ast.unparse(k.value) for k in call.keywords}
        assert keywords.get("proxy") == "None"
        assert keywords.get("create_connection", "").startswith("pinned_connection(pin)")
