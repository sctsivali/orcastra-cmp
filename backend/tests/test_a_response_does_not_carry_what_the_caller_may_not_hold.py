"""Two surfaces returned more than the caller was entitled to, and one let a caller widen scope.

`GET /server`, `GET /server/{node}` and `GET /server/{node}/config` returned the LXD daemon's raw
`environment` and `config` blocks. The environment block carries the cluster's certificate PEM,
its fingerprint and every member's listen address; the config block is where an LXD daemon keeps
`core.trust_password`. All three sit on a `require_tenant()` router, so a tenant with one assigned
cluster received all of it.

The project had already written the allowlist for exactly this data, with a comment naming the
certificate, and applied it on one path only. The page that renders the environment filtered
certificate keys in the browser, which narrows what is drawn and not what is sent.

`POST /organizations/{org_id}/clusters` is the third. It checked that the caller had access to the
ORGANISATION, which is their own, and never checked the CLUSTER. Org scope is then derived from
the row it writes, so a partner could bind another tenant's cluster to an org they control and
inherit full scope over it, with break-glass treating the cluster as owned rather than foreign.
"""

import ast
import inspect
from pathlib import Path

import pytest

from app.routers import lxd_server, organizations
from app.services.lxd.resources import (
    PUBLIC_CONFIG_KEYS,
    PUBLIC_ENVIRONMENT_KEYS,
    public_config,
    public_environment,
)

# A realistic LXD server payload: the harmless descriptors alongside the material that must not
# leave. Key names are the ones LXD actually uses.
RAW_ENVIRONMENT = {
    "server_version": "5.21.1",
    "kernel": "Linux",
    "certificate": "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----",
    "certificate_fingerprint": "a3f9" * 16,
    "addresses": ["10.0.0.5:8443", "100.73.127.75:8443"],
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
    """Two vocabularies, deliberately separate: config keys are dotted, environment keys are not."""
    assert not (PUBLIC_CONFIG_KEYS & PUBLIC_ENVIRONMENT_KEYS)
    assert all("." in key for key in PUBLIC_CONFIG_KEYS)


def test_every_server_response_is_filtered() -> None:
    """Structural, because the leak was three call sites and only one of them was ever fixed.

    A new handler in this router that returns `server_data.get('config'|'environment')` raw is the
    same defect again, and nothing at runtime would say so.
    note: scoped to what is RETURNED, not to every mention. Two call sites build a local
    `metadata` dict from the raw environment purely to feed
    `detect_cluster_type_from_metadata`, which answers with a type string and never puts the
    block on the wire. Flagging those would be a false positive that teaches people to silence
    this test.
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
    # The check must come before the write, or it is decoration.
    assert source.index("get_owned_clusters_ignoring_admin") < source.index("svc.bind_cluster"), (
        "the ownership check runs after the binding is written"
    )


def test_the_cluster_check_does_not_apply_to_admin() -> None:
    """Admin binds on behalf of partners during onboarding; narrowing that would break setup."""
    source = inspect.getsource(organizations.bind_cluster)
    assert 'if role != "admin"' in source
