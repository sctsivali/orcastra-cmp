"""The /server routes return an allowlisted config and environment, never the raw LXD blocks.

The daemon's config block is where LXD keeps core.trust_password, OIDC and Loki credentials and
the OVN client key, and the environment block carries the cluster certificate, its fingerprint,
every listen address and the server PID. GET /server, GET /server/{node} and
GET /server/{node}/config returned both whole.
"""

import ast
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.routers import lxd_server
from app.services.lxd.resources import PUBLIC_CONFIG_KEYS, public_config

SECRET_CONFIG = {
    "core.https_address": "[::]:8443",
    "core.trust_password": "hunter2",
    "oidc.client.secret": "oidc-secret",
    "loki.auth.password": "loki-secret",
    "network.ovn.client_key": "-----BEGIN PRIVATE KEY-----",
    "images.auto_update_interval": "6",
}
SECRET_ENVIRONMENT = {
    "server_name": "member-1",
    "server_version": "6.1",
    "certificate": "-----BEGIN CERTIFICATE-----",
    "certificate_fingerprint": "abcd" * 16,
    "addresses": ["192.0.2.10:8443"],
    "server_pid": 4242,
}
SECRET_KEYS = {"core.trust_password", "oidc.client.secret", "loki.auth.password", "network.ovn.client_key"}
SECRET_ENV_KEYS = {"certificate", "certificate_fingerprint", "addresses", "server_pid"}


def test_public_config_keeps_only_the_allowlist() -> None:
    filtered = public_config(SECRET_CONFIG)
    assert filtered == {
        "core.https_address": "[::]:8443",
        "images.auto_update_interval": "6",
    }
    assert not SECRET_KEYS & set(filtered)
    assert set(filtered) <= PUBLIC_CONFIG_KEYS


@pytest.mark.parametrize("empty", [None, {}])
def test_public_config_passes_an_empty_block_through(empty: Optional[dict]) -> None:
    assert public_config(empty) == empty


class _FakeClient:
    host_info = {
        "api_version": "1.0",
        "auth": "trusted",
        "api_extensions": [],
        "config": dict(SECRET_CONFIG),
        "environment": dict(SECRET_ENVIRONMENT),
        "public": False,
    }


def _fake_service() -> Any:
    return SimpleNamespace(
        connection_manager=SimpleNamespace(get_client=lambda _node: _FakeClient())
    )


@pytest.fixture(autouse=True)
def no_metadata_store(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SimpleNamespace(get_node=lambda _node: None)
    monkeypatch.setattr(lxd_server, "get_node_metadata_store", lambda: store)


def _assert_public(config: dict, environment: dict) -> None:
    assert not SECRET_KEYS & set(config)
    assert set(config) <= PUBLIC_CONFIG_KEYS
    assert "core.https_address" in config
    assert not SECRET_ENV_KEYS & set(environment)
    assert environment["server_name"] == "member-1"


def test_the_fan_out_row_is_filtered() -> None:
    read = lxd_server._fetch_server_info_for_node(_fake_service(), "cluster-a")
    row = read.rows[0]
    _assert_public(row.config, row.environment)


def test_the_single_node_read_is_filtered() -> None:
    info = asyncio.run(lxd_server.get_server_info("cluster-a", lxd_service=_fake_service()))
    _assert_public(info.config, info.environment)


def test_the_config_read_is_filtered() -> None:
    config = asyncio.run(lxd_server.get_server_config("cluster-a", lxd_service=_fake_service()))
    assert not SECRET_KEYS & set(config)
    assert set(config) <= PUBLIC_CONFIG_KEYS


def _wrapped_by(tree: ast.AST, source: str) -> list[str]:
    """Each `server_data.get('config'|'environment', ...)` that leaves the handler, with its wrapper."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    problems = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and ast.unparse(node.func.value) == "server_data"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in ("config", "environment")
        ):
            continue
        key = node.args[0].value
        parent = parents[node]
        while isinstance(parent, ast.BoolOp):
            parent = parents[parent]
        # note: the environment also feeds type detection inside a local metadata dict, which
        # never reaches the response
        if isinstance(parent, ast.Dict):
            continue
        wrapper = f"public_{key}"
        if not (isinstance(parent, ast.Call) and ast.unparse(parent.func) == wrapper):
            problems.append(f"line {node.lineno}: {ast.unparse(node)} is not inside {wrapper}(")
    return problems


def test_every_config_and_environment_leaving_the_router_is_wrapped() -> None:
    source = Path(inspect.getfile(lxd_server)).read_text(encoding="utf-8")
    assert _wrapped_by(ast.parse(source), source) == []
