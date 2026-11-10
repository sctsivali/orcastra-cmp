"""A list that names a missing cluster says why, and says it only to who may hear it.

A cluster the breaker skipped, or one this worker holds no client for, needs a reason as much as
one past the inventory wall. The text is an operator diagnosis, so a Tenant gets none of it, and on
a cluster shared with another organization it must not name what that organization owns.
"""

import ast
from pathlib import Path

import pytest

from app.core import org_scope
from app.services.lxd import health
from app.services.lxd.fanout import complete_reasons

APP = Path(__file__).resolve().parents[1] / "app"
ROUTERS = APP / "routers"
RAW_FAILURE = "HTTPSConnectionPool(host='10.0.0.1', port=8443): Max retries exceeded"


@pytest.fixture(autouse=True)
def clean_health():
    for cluster in ("down", "refused", "fresh", "shared", "own"):
        health.clear(cluster)
    yield
    for cluster in ("down", "refused", "fresh", "shared", "own"):
        health.clear(cluster)


def test_a_cluster_with_no_recorded_reason_still_gets_one():
    health.record_failure("down", RAW_FAILURE)

    reasons = complete_reasons(["down", "fresh"], {})

    assert reasons == {
        "down": "unreachable: no connection to the cluster",
        "fresh": "unreachable: not connected from this server yet",
    }


def test_raw_connection_text_never_reaches_the_answer():
    health.record_failure("down", RAW_FAILURE)

    assert "10.0.0.1" not in complete_reasons(["down"], {})["down"]


def test_a_certificate_refusal_is_not_reported_as_an_outage():
    health.record_failure("refused", "not authorized", kind=health.KIND_UNAUTHORIZED)

    assert complete_reasons(["refused"], {}) == {
        "refused": "forbidden: certificate not trusted by the cluster"
    }


def test_a_reason_the_listing_already_found_is_kept():
    known = {"down": "unreachable: did not answer within 30s"}

    assert complete_reasons(["down"], known) == known


def test_a_partial_cluster_is_not_reported_as_unreachable():
    reasons = complete_reasons([], {}, partial=["fresh"])

    assert reasons == {"fresh": "partial: some rows could not be read"}


def test_a_tenant_gets_no_reason_at_all():
    token = org_scope._tenant_acl_var.set({"rules": []})
    try:
        assert complete_reasons(["down"], {"down": "unreachable: read timed out"}) == {}
    finally:
        org_scope._tenant_acl_var.reset(token)


def test_a_shared_cluster_keeps_the_other_organizations_names_out_of_the_text():
    known = {
        "shared": "partial: volume payroll-db in pool fast: ValueError",
        "own": "partial: volume web in pool fast: ValueError",
    }
    token = org_scope._visible_projects_var.set({"shared": {"my-project"}})
    try:
        reasons = complete_reasons(["shared", "own"], known)
    finally:
        org_scope._visible_projects_var.reset(token)

    assert reasons["shared"] == "partial: some rows could not be read"
    assert reasons["own"] == known["own"], "a cluster this caller alone holds keeps its diagnosis"


def test_lxd_error_text_is_withheld_on_a_shared_cluster_too():
    known = {
        "shared": "forbidden: LXD answered 403: project payroll is restricted",
        "own": "error: LXD answered 500: volume payroll-db is busy",
    }
    token = org_scope._visible_projects_var.set({"shared": {"a"}, "own": {"b"}})
    try:
        reasons = complete_reasons(["shared", "own"], known)
    finally:
        org_scope._visible_projects_var.reset(token)

    assert reasons == {
        "shared": "forbidden: the cluster refused this read",
        "own": "error: the cluster could not answer this read",
    }


def _is_helper(value: ast.expr) -> bool:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "complete_reasons"
    )


def _reason_answers(path: Path):
    """Each `node_errors` a response is given in this file, and whether the helper produced it."""
    tree = ast.parse(path.read_text())
    bound = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and _is_helper(node.value)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            values = [keyword.value for keyword in node.keywords if keyword.arg == "node_errors"]
        elif isinstance(node, ast.Dict):
            values = [
                value
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and key.value == "node_errors"
            ]
        else:
            continue
        for value in values:
            helped = _is_helper(value) or (isinstance(value, ast.Name) and value.id in bound)
            yield f"{path.relative_to(APP)}:{value.lineno}", helped


def test_every_node_errors_a_response_carries_comes_from_the_helper():
    answers = [answer for path in sorted(APP.rglob("*.py")) for answer in _reason_answers(path)]

    assert [site for site, helped in answers if not helped] == []
    # A walk over nothing passes for the wrong reason.
    assert len(answers) >= 16
