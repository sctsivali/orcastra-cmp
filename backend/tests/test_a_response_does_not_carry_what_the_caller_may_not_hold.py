"""Responses carry only what the caller may hold, and binding a cluster cannot widen scope.

- `GET /server`, `/server/{node}` and `/server/{node}/config` (a `require_tenant()` router) pass
  server-side allowlists: LXD's `environment` carries the cluster certificate PEM, fingerprint and
  member addresses, and `config` holds `core.trust_password`. A browser filter leaves them in the
  response.
- `POST /organizations/{org_id}/clusters` checks the CLUSTER too: org scope derives from the row it
  writes, so an unchecked cluster id would let a partner bind another tenant's cluster.
- No console REST route hands out a console operation: its per-fd secrets in `ws_url` and
  `control_url` open a raw LXD console channel that bypasses the proxy, session tracking, the kill
  paths and the break-glass watchdog.
"""

import ast
import inspect
from pathlib import Path

import pytest

from app.routers import console, lxd_server, organizations
from app.services.lxd.resources import (
    PUBLIC_CONFIG_KEYS,
    PUBLIC_ENVIRONMENT_KEYS,
    public_config,
    public_environment,
)

# An LXD server payload: harmless descriptors beside material that must not leave. Key names
# match the ones LXD uses.
RAW_ENVIRONMENT = {
    "server_version": "5.21.1",
    "kernel": "Linux",
    "certificate": "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----",
    "certificate_fingerprint": "a3f9" * 16,
    "addresses": ["10.0.0.5:8443", "100.64.0.10:8443"],
    "server_pid": 4242,
}

RAW_CONFIG = {
    "core.https_address": "[::]:8443",
    "core.trust_password": "true",
    "cluster.join_token_expiry": "3h",
    "oidc.client.secret": "hunter2",
    "images.auto_update_interval": "6",
}


@pytest.mark.parametrize("leaky", ["certificate", "certificate_fingerprint", "addresses", "server_pid"])
def test_the_environment_allowlist_drops_the_material_that_matters(leaky: str) -> None:
    assert leaky not in public_environment(RAW_ENVIRONMENT)


def test_the_environment_allowlist_keeps_what_the_page_needs() -> None:
    kept = public_environment(RAW_ENVIRONMENT)
    assert kept["server_version"] == "5.21.1"
    assert kept["kernel"] == "Linux"


@pytest.mark.parametrize("leaky", ["core.trust_password", "cluster.join_token_expiry", "oidc.client.secret"])
def test_the_config_allowlist_drops_the_secrets_an_lxd_daemon_keeps_there(leaky: str) -> None:
    assert leaky not in public_config(RAW_CONFIG)


def test_the_config_allowlist_keeps_the_operational_settings() -> None:
    kept = public_config(RAW_CONFIG)
    assert kept["core.https_address"] == "[::]:8443"
    assert kept["images.auto_update_interval"] == "6"


def test_an_unknown_config_key_is_dropped_rather_than_passed_through() -> None:
    """Allowlist, so a key LXD adds in a future release is silent rather than a new leak."""
    assert public_config({"some.future.secret": "x"}) == {}
    assert public_environment({"some_future_secret": "x"}) == {}


def test_the_allowlists_do_not_overlap_by_accident() -> None:
    """Config keys are dotted and environment keys are not, so the two sets never share a key."""
    assert not (PUBLIC_CONFIG_KEYS & PUBLIC_ENVIRONMENT_KEYS)
    assert all("." in key for key in PUBLIC_CONFIG_KEYS)


def test_every_server_response_is_filtered() -> None:
    """No handler in this router returns `server_data.get('config'|'environment')` raw.

    Only returned values are checked. Some call sites build a local `metadata` dict from the raw
    environment to feed `detect_cluster_type_from_metadata`, which answers with a type string and
    never puts the block on the wire.
    """
    tree = ast.parse(Path(inspect.getfile(lxd_server)).read_text())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        rendered = ast.unparse(node.value)
        for block in ("'config'", "'environment'", '"config"', '"environment"'):
            if f"server_data.get({block}" in rendered and "public_" not in rendered:
                offenders.append(f"lxd_server.py:{node.lineno}: {rendered[:90]}")
    assert not offenders, (
        "these return the raw LXD block, which carries the cluster certificate and "
        "core.trust_password:\n  " + "\n  ".join(offenders)
    )


# ── binding a cluster to an organization ───────────────────────────────────────────────────────

def test_binding_a_cluster_checks_the_cluster_and_not_only_the_organization() -> None:
    """`user_has_access` answers about the org, which is the caller's own. It is not the boundary.

    Org scope is derived from the row this handler writes, so an unchecked cluster id here is
    self-service scope expansion onto someone else's cluster.
    """
    tree = ast.parse(inspect.getsource(organizations.bind_cluster).lstrip())
    source = ast.unparse(tree)
    assert "get_owned_clusters_ignoring_admin" in source, (
        "bind_cluster does not check that the caller owns body.cluster_id"
    )
    # The check must run before the write.
    assert source.index("get_owned_clusters_ignoring_admin") < source.index("svc.bind_cluster"), (
        "the ownership check runs after the binding is written"
    )


def test_the_cluster_check_does_not_apply_to_admin() -> None:
    """Admin binds on behalf of partners during onboarding; narrowing that would break setup."""
    source = inspect.getsource(organizations.bind_cluster)
    assert 'if role != "admin"' in source


# ── the graphics console session ───────────────────────────────────────────────────────────────


def test_no_console_route_hands_out_a_console_operation() -> None:
    """A console operation's per-fd secrets are a bearer credential for the guest's screen.

    A REST route would have to create an operation and could only withhold its secrets, orphaning
    an operation on every call. The console runs on the WebSocket, which never discloses its
    operation, and no REST route of the console router may create one.
    """
    offenders = [
        f"{sorted(route.methods or [])} {route.path}"
        for route in console.router.routes
        if getattr(route, "methods", None) and route.path.endswith("/session")
    ]
    assert not offenders, offenders
    source = Path(inspect.getfile(console)).read_text()
    assert "open_console_operation" not in source, "the console router must not start operations itself"
