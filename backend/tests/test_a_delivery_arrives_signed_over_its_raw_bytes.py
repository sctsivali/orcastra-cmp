"""A delivery has to leave this process, cross a socket, and verify on the other side.

Everything else about the webhook subsystem can be asserted against fakes. This cannot: the
property that matters is that the bytes a receiver reads are the bytes we signed, and a fake
transport is precisely where that stops being true. So this file stands up a real HTTP server on
loopback and sends a real request through the real client.

Two receiver bugs are the reason it is written this way, and both are documented as rules rather
than discovered later:

  * verifying against a re-serialised body instead of the raw bytes. The sender emits compact
    separators and sorted keys, so `json.dumps(json.loads(body))` differs from what was signed in
    almost every language's defaults. Asserted below in both directions.
  * accepting any timestamp. A signature captured from a receiver's own logs otherwise replays
    forever.

The delivery address is loopback, which the SSRF filter refuses by design. That is not worked
around here: the DEBUG-gated exemption is switched on for the test the same way it is for the QA
harness, and a companion test asserts the exemption produces nothing when DEBUG is off.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List

import pytest

from app.core import config
from app.core.safe_http import SafeHTTPError, post_pinned
from app.services.webhooks.signing import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    secret_fingerprint,
    sign,
    signature_header,
    verify,
)

SECRET = "whsec_test_secret_value"
TIMESTAMP = 1757000000


class _Captured:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []


CAPTURED = _Captured()
RESPOND_WITH = {"status": 200}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length)
        # important: the RAW bytes are captured before anything parses them. A receiver that
        # parses first has already lost the thing it needs to verify.
        CAPTURED.requests.append(
            {"raw": raw, "headers": {k.lower(): v for k, v in self.headers.items()}}
        )
        status = RESPOND_WITH["status"]
        body = RESPOND_WITH.get("body", b"ok")
        self.send_response(status)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture(scope="module")
def receiver():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hooks"
    server.shutdown()


@pytest.fixture
def dev_deployment(monkeypatch):
    """Loopback is refused by design, so the delivery test opts in the way the QA harness does."""
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("WEBHOOK_DEST_ALLOWLIST_CIDRS", "127.0.0.0/8")
    monkeypatch.setenv("WEBHOOK_ALLOW_HTTP", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://t:t@localhost:5432/t")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _send(url: str, payload: Dict[str, Any]):
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    headers = {
        "content-type": "application/json",
        SIGNATURE_HEADER: signature_header([SECRET], TIMESTAMP, body),
        TIMESTAMP_HEADER: str(TIMESTAMP),
        EVENT_ID_HEADER: payload["id"],
    }
    return asyncio.run(
        post_pinned(url, content=body, headers=headers, max_response_bytes=4096)
    ), body


def test_a_signed_delivery_arrives_and_verifies_over_the_raw_bytes(receiver, dev_deployment):
    CAPTURED.requests.clear()
    RESPOND_WITH["status"] = 200

    payload = {
        "specversion": "1.0",
        "id": "evt_0123456789abcdef",
        "type": "orcastra.instance.created.v1",
        "data": {"cluster_id": "nuc2", "resource_name": "web-01"},
    }
    (status, _head, _seen), sent_body = _send(receiver, payload)

    assert status == 200
    assert len(CAPTURED.requests) == 1
    received = CAPTURED.requests[0]

    assert received["raw"] == sent_body, "the bytes on the wire are not the bytes we signed"
    assert verify(
        SECRET,
        received["headers"][SIGNATURE_HEADER.lower()],
        int(received["headers"][TIMESTAMP_HEADER.lower()]),
        received["raw"],
        now=TIMESTAMP,
    ), "a receiver following the documented recipe could not verify the delivery"


def test_verifying_a_re_serialised_body_fails(receiver, dev_deployment):
    """The single most common receiver bug, asserted so the documentation can state it as a rule."""
    CAPTURED.requests.clear()
    RESPOND_WITH["status"] = 200
    payload = {"specversion": "1.0", "id": "evt_reserialise", "type": "x", "data": {}}
    _send(receiver, payload)

    received = CAPTURED.requests[0]
    reserialised = json.dumps(json.loads(received["raw"])).encode("utf-8")
    assert reserialised != received["raw"], "the test needs a body that changes when reparsed"
    assert not verify(
        SECRET,
        received["headers"][SIGNATURE_HEADER.lower()],
        TIMESTAMP,
        reserialised,
        now=TIMESTAMP,
    )


def test_a_captured_delivery_cannot_be_replayed_later(receiver, dev_deployment):
    CAPTURED.requests.clear()
    RESPOND_WITH["status"] = 200
    _send(receiver, {"specversion": "1.0", "id": "evt_replay", "type": "x", "data": {}})
    received = CAPTURED.requests[0]
    header = received["headers"][SIGNATURE_HEADER.lower()]

    assert verify(SECRET, header, TIMESTAMP, received["raw"], now=TIMESTAMP + 299)
    assert not verify(SECRET, header, TIMESTAMP, received["raw"], now=TIMESTAMP + 301)


def test_a_receiver_that_fails_is_reported_rather_than_swallowed(receiver, dev_deployment):
    CAPTURED.requests.clear()
    RESPOND_WITH["status"] = 500
    (status, _head, _seen), _body = _send(
        receiver, {"specversion": "1.0", "id": "evt_fail", "type": "x", "data": {}}
    )
    RESPOND_WITH["status"] = 200
    assert status == 500, "a failing receiver must surface as a status, not as success"


def test_the_response_body_is_capped_while_streaming(receiver, dev_deployment):
    """What a receiver says is diagnostic. It must not decide how much memory a worker uses."""
    CAPTURED.requests.clear()
    RESPOND_WITH["status"] = 200
    RESPOND_WITH["body"] = b"x" * 200_000
    try:
        (status, head, seen), _ = _send(
            receiver, {"specversion": "1.0", "id": "evt_big", "type": "x", "data": {}}
        )
        assert status == 200
        assert len(head) <= 64 * 1024, "more of the response was kept than the cap allows"
        assert seen > len(head), "the cap did not actually stop short of the full body"
    finally:
        RESPOND_WITH["body"] = b"ok"


def test_the_signature_carries_its_version_and_key_label(receiver, dev_deployment):
    """Both are inside the signed material, so neither can be stripped by a lenient verifier."""
    entry = sign(SECRET, TIMESTAMP, b"{}")
    assert entry.startswith("v1,k=")
    assert secret_fingerprint(SECRET) in entry
    assert not entry.endswith(SECRET)


def test_loopback_is_refused_when_the_deployment_is_not_a_development_one(receiver, monkeypatch):
    """The exemption is gated on DEBUG in code, so filling the variable in production does nothing."""
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("WEBHOOK_DEST_ALLOWLIST_CIDRS", "127.0.0.0/8")
    monkeypatch.setenv("WEBHOOK_ALLOW_HTTP", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://t:t@localhost:5432/t")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost")
    config.get_settings.cache_clear()
    try:
        assert config.webhook_destination_exemptions() == ()
        assert config.webhook_requires_https() is True
        with pytest.raises(SafeHTTPError):
            _send(receiver, {"specversion": "1.0", "id": "evt_prod", "type": "x", "data": {}})
    finally:
        config.get_settings.cache_clear()


# ── rotation has to be survivable ──────────────────────────────────────────────────────────────

class _RotatingVault:
    """Just enough Vault to hold one installation's signing material."""

    def __init__(self) -> None:
        self.store: dict = {}

    def vault_read(self, path: str):
        return self.store.get(path)

    def vault_kv_put(self, path: str, payload: dict) -> None:
        self.store[path] = dict(payload)


def test_a_rotation_keeps_the_old_secret_valid_for_a_window() -> None:
    """The header is a list so a receiver can switch at its own pace, and nothing used that.

    Only one secret was ever stored, rotation overwrote it in place, and the sender sent one
    entry. So every receiver that had not already been handed the new value began rejecting on
    the very next delivery and walked toward AUTO_SUSPEND_AFTER. That is the exact failure the
    list format exists to prevent, built and never connected.
    """
    from app.services.extension_credentials import (
        read_signing_secrets,
        rotate_signing_secret,
    )

    vault = _RotatingVault()
    old = rotate_signing_secret(vault, 42)
    new = rotate_signing_secret(vault, 42)
    assert old != new

    live = read_signing_secrets(vault, 42)
    assert new in live, "the new secret must sign"
    assert old in live, "a receiver still holding the old secret must survive the rotation"

    header = signature_header(live, TIMESTAMP, b"{}")
    assert verify(old, header, TIMESTAMP, b"{}", now=TIMESTAMP), "the old secret must verify"
    assert verify(new, header, TIMESTAMP, b"{}", now=TIMESTAMP), "and so must the new one"


def test_the_overlap_ends(monkeypatch) -> None:
    """The positive twin above would pass just as well if the old secret were kept forever, which
    is not a rotation. This is the half that makes it one."""
    from app.services.extension_credentials import (
        SIGNING_OVERLAP_SECONDS,
        read_signing_secrets,
        rotate_signing_secret,
    )

    vault = _RotatingVault()
    old = rotate_signing_secret(vault, 43)
    new = rotate_signing_secret(vault, 43)

    later = __import__("time").time() + SIGNING_OVERLAP_SECONDS + 1
    live = read_signing_secrets(vault, 43, now=later)
    assert live == [new], "the superseded secret must stop being honoured once the window passes"
    assert old not in live
