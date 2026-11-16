"""A cluster registered without a port is served on 443, and the file tunnel must dial that socket.

The REST client, the terminal's websockets and the pin probe all follow the scheme's default
port. The file tunnel used to fall back to LXD's 8443 instead, so on a cluster registered as
`https://lxd.example.org` the version check and the instance read passed on 443, the tunnel dialled
8443, and the explorer said "Cluster not reachable" while the shell on the same instance worked.
"""

import logging
import ssl
from types import SimpleNamespace

import pytest

from app.services.instance_files import tunnel
from app.services.instance_files.errors import GuestFileError

PIN = "ab" * 32


def _patch_until_dial(monkeypatch, base: str) -> list:
    """Every step before the dial passes; the dial is recorded and refused."""
    dialled: list = []

    async def supported(host):
        return None

    async def running(host, project, instance_name):
        return {"status_code": tunnel.STATUS_RUNNING, "type": "container"}

    async def material(host):
        return base, ssl.create_default_context(), PIN

    async def refuse(host, port, **kwargs):
        dialled.append((host, port))
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(tunnel, "_require_supported_server", supported)
    monkeypatch.setattr(tunnel, "_running_record", running)
    monkeypatch.setattr(tunnel, "_material", material)
    monkeypatch.setattr(tunnel.asyncio, "open_connection", refuse)
    return dialled


async def _open(host: str = "central-2") -> GuestFileError:
    with pytest.raises(GuestFileError) as caught:
        async with tunnel.open_guest(host, "default", "vm1"):
            pass
    return caught.value


@pytest.mark.parametrize(
    "base, expected",
    [
        ("wss://lxd.example.org", ("lxd.example.org", 443)),
        ("wss://lxd.example.org:8443", ("lxd.example.org", 8443)),
        ("wss://10.0.0.5:9443", ("10.0.0.5", 9443)),
    ],
)
async def test_the_tunnel_dials_the_port_every_other_path_uses(monkeypatch, base, expected):
    dialled = _patch_until_dial(monkeypatch, base)
    failure = await _open()
    assert dialled == [expected]
    assert failure.code == "cluster_unreachable"


async def test_a_refused_dial_names_the_socket_and_the_reason_in_the_log(monkeypatch, caplog):
    _patch_until_dial(monkeypatch, "wss://lxd.example.org")
    caplog.set_level(logging.INFO, logger=tunnel.logger.name)
    await _open("central-2")
    line = " ".join(record.getMessage() for record in caplog.records)
    assert "central-2" in line and "lxd.example.org:443" in line and "Connection refused" in line


def test_a_registered_portless_endpoint_is_the_daemon_socket_on_443(monkeypatch):
    """The raw-proxy pull check compares sockets, so a portless registration means 443 there too."""
    from app.core import key_pull_sources

    vault = SimpleNamespace(vault_read=lambda path, use_cache=True: {"endpoint": "https://lxd.example.org"})
    manager = SimpleNamespace(list_clusters=lambda: ["portless"], vault=vault, clusters_path="secret/data/clusters")
    monkeypatch.setattr("app.services.lxd_service.get_lxd_service", lambda: SimpleNamespace(cert_manager=manager))
    assert key_pull_sources._is_registered_cluster("https://lxd.example.org") is True
    assert key_pull_sources._is_registered_cluster("https://lxd.example.org:443") is True
    assert key_pull_sources._is_registered_cluster("https://lxd.example.org:8443") is False


class _Reader:
    def __init__(self, body: bytes):
        self._body = body

    async def read(self, size: int) -> bytes:
        return self._body[:size]


def _head(code: int, body: bytes) -> bytes:
    return f"HTTP/1.1 {code} X\r\nContent-Length: {len(body)}\r\n\r\n".encode()


async def test_a_proxy_error_in_front_of_a_vm_is_not_blamed_on_the_guest_agent():
    body = b"<html><body>502 Bad Gateway</body></html>"
    failure = await tunnel._refused(_Reader(body), _head(502, body), 502, {"type": "virtual-machine"})
    assert failure.code == "guest_io_error"


@pytest.mark.parametrize(
    "body",
    [b'{"error":"VM agent isn\'t currently running","error_code":500}', b""],
)
async def test_a_vm_whose_agent_is_down_still_reads_as_agent_unavailable(body):
    failure = await tunnel._refused(_Reader(body), _head(500, body), 500, {"type": "virtual-machine"})
    assert failure.code == "agent_unavailable"


def _environment(version: str = "5.21.8"):
    payload = {"metadata": {"environment": {"server": "lxd", "server_version": version}}}
    return SimpleNamespace(api=SimpleNamespace(get=lambda: SimpleNamespace(json=lambda: payload)))


async def _direct(fn, *args, label=None, host=None):
    return fn(*args)


async def test_a_stale_pooled_client_is_rebuilt_before_the_version_is_judged(monkeypatch):
    from app.services import lxd_ws

    def stale_get():
        raise ConnectionResetError("pooled connection was closed")

    stale = SimpleNamespace(api=SimpleNamespace(get=stale_get))
    monkeypatch.setattr(lxd_ws, "lxd_client", lambda host: stale)
    monkeypatch.setattr(lxd_ws, "client_for_retry", lambda host: _environment())
    monkeypatch.setattr(lxd_ws, "session_io", _direct)
    tunnel._server_checks.pop("stale-host", None)
    await tunnel._require_supported_server("stale-host")
    assert tunnel._server_checks["stale-host"][1].allowed


async def test_no_connected_client_is_logged_as_such(monkeypatch, caplog):
    from app.services import lxd_ws

    monkeypatch.setattr(lxd_ws, "lxd_client", lambda host: None)
    monkeypatch.setattr(lxd_ws, "client_for_retry", lambda host: None)
    monkeypatch.setattr(lxd_ws, "session_io", _direct)
    tunnel._server_checks.pop("down-host", None)
    caplog.set_level(logging.INFO, logger=tunnel.logger.name)
    with pytest.raises(GuestFileError) as caught:
        await tunnel._require_supported_server("down-host")
    assert caught.value.code == "cluster_unreachable"
    line = " ".join(record.getMessage() for record in caplog.records)
    assert "down-host" in line and "no connected client" in line


def test_no_lxd_dial_site_defaults_a_port_to_8443():
    """One rule for every socket: `endpoint_host_port`. A literal `or 8443` is how this broke."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = [
        f"{path.relative_to(root)}:{number}"
        for path in root.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\bor\s+8443\b", line)
    ]
    assert offenders == []


def test_the_shared_port_rule_knows_websocket_schemes():
    from app.services.lxd_certs.pinning import endpoint_host_port

    assert endpoint_host_port("wss://lxd.example.org") == ("lxd.example.org", 443)
    assert endpoint_host_port("ws://lxd.example.org") == ("lxd.example.org", 80)
    assert endpoint_host_port("wss://lxd.example.org:8443") == ("lxd.example.org", 8443)

