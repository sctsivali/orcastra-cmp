"""Structural guards for certificate changes: one at a time per cluster, audited, unscoped sweeps.

Two renewals racing on a cluster each add their certificate to trust and only one survives in
Vault, leaving a trusted certificate nothing names. Every path that changes a cluster's client
certificate therefore takes the cluster's certificate lock, and a request that meets the lock
answers 409 instead of waiting behind it.
"""

import ast
from pathlib import Path

from app.services.activity_logger import ACTION_CATEGORIES, HIGH_SEVERITY_ACTIONS
from tests.test_a_background_run_does_not_inherit_a_request_scope import FORBIDDEN, _names

APP = Path(__file__).resolve().parents[1] / "app"
ROTATION = APP / "services" / "lxd_certs" / "rotation.py"
PENDING = APP / "services" / "lxd_certs" / "pending.py"
CLEANUP = APP / "services" / "lxd_certs" / "trust_cleanup.py"
STORE = APP / "services" / "lxd_certs" / "trust_cleanup_store.py"
HTTP = APP / "services" / "lxd_certs" / "trust_http.py"
ROUTER = APP / "routers" / "cert_manager.py"
REGISTRATION = APP / "routers" / "cluster_registration.py"


def _function(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path.name}")


def _mentions(node: ast.AST, name: str) -> bool:
    return any(found == name for found, _ in _names(node))


def test_every_rotation_entry_point_takes_the_cluster_lock():
    for name in ("renew", "verify", "cancel"):
        assert _mentions(_function(ROTATION, name), "cluster_cert_lock"), name
    assert _mentions(_function(PENDING, "regenerate"), "cluster_cert_lock")
    assert _mentions(_function(CLEANUP, "sweep_cluster"), "cluster_cert_lock")


def test_regenerate_and_delete_take_the_cluster_lock():
    assert _mentions(_function(REGISTRATION, "regenerate_certificate"), "regenerate")
    for name in ("delete_cluster", "register_cluster"):
        assert _mentions(_function(REGISTRATION, name), "cluster_cert_lock"), name


def test_regenerate_never_overwrites_the_active_certificate():
    """It issues a pending certificate; only Verify, once the cluster trusts it, replaces /certs."""
    names = {name for name, _ in _names(_function(REGISTRATION, "regenerate_certificate"))}
    assert "generate_and_store_certificate" not in names
    source = ast.unparse(_function(PENDING, "regenerate"))
    assert "pending_certs" in source and "_activate" in source, "activates only when nothing is active"


def test_every_mutating_route_answers_409_when_the_cluster_is_busy():
    for name in ("initiate_renewal", "verify_renewal", "cancel_renewal", "remove_superseded_trust"):
        assert _mentions(_function(ROUTER, name), "CertificateOperationBusy"), name
    for name in ("regenerate_certificate", "delete_cluster"):
        assert _mentions(_function(REGISTRATION, name), "CertificateOperationBusy"), name


def test_every_route_that_takes_a_cluster_checks_access_first():
    for name in ("get_certificate", "get_pending_certificate", "initiate_renewal", "verify_renewal",
                 "cancel_renewal", "remove_superseded_trust"):
        assert _mentions(_function(ROUTER, name), "verify_cluster_access"), name


def test_the_overwrite_comes_after_a_committed_record():
    """In renew and verify the record is committed (`_record`, its own transaction) before the
    call that overwrites /certs; regenerate and re-registration record before generating."""
    for name in ("renew", "verify"):
        source = ast.unparse(_function(ROTATION, name))
        assert source.index("_record(") < source.index("_activate"), name
    assert "record_superseded_now" in ast.unparse(_function(ROTATION, "_record"))
    source = ast.unparse(_function(REGISTRATION, "register_cluster"))
    assert source.index("record_current_superseded") < source.index("generate_and_store_certificate")


def _emitted(paths, prefix="cluster.certificate."):
    found = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(prefix):
                found.add(node.value)
    return found


def test_every_certificate_action_is_registered_and_the_revoking_ones_are_high():
    emitted = _emitted([ROTATION, PENDING, CLEANUP, REGISTRATION])
    assert emitted >= {
        "cluster.certificate.renew",
        "cluster.certificate.activate",
        "cluster.certificate.cancel",
        "cluster.certificate.regenerate",
        "cluster.certificate.trust_remove",
    }, emitted
    assert not sorted(a for a in emitted if a not in ACTION_CATEGORIES)
    assert {"cluster.certificate.regenerate", "cluster.certificate.trust_remove"} <= HIGH_SEVERITY_ACTIONS


def test_the_sweep_names_no_context_scoped_helper():
    """The loop runs outside any request, where an unset org-scope ContextVar means "allowed"."""
    offenders = [
        f"{path.name}:{line} names {name}"
        for path in (CLEANUP, STORE, HTTP)
        for name, line in _names(ast.parse(path.read_text()))
        if name in FORBIDDEN
    ]
    assert not offenders, offenders


def test_the_sweep_never_reaches_for_the_pooled_client():
    """Proving trust through get_client proves the certificate a worker holds, which after a
    rotation is the old one; removing on that basis can lock the dashboard out."""
    for path in (CLEANUP, HTTP):
        names = {name for name, _ in _names(ast.parse(path.read_text()))}
        assert not names & {"get_client", "materialize", "ensure_connection"}, path.name
