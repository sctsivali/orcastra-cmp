"""A list that names a missing cluster says why, and says it only to who may hear it.

Six of seven rows in the Instances banner read "no reason reported" because only a cluster past
the inventory wall carried a reason; one the breaker skipped, or that this worker holds no client
for, carried nothing. The text is an operator diagnosis, so a Tenant gets none of it, and on a
cluster shared with another organization it must not name what that organization owns.
"""

import re
from pathlib import Path

import pytest

from app.core import org_scope
from app.services.lxd import health
from app.services.lxd.fanout import complete_reasons

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"
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


@pytest.mark.parametrize(
    "router",
    sorted(path.name for path in ROUTERS.glob("*.py") if "unreachable_nodes=" in path.read_text()),
)
def test_every_router_that_names_a_missing_cluster_goes_through_the_helper(router):
    source = (ROUTERS / router).read_text()

    responses = len(re.findall(r"^\s+unreachable_nodes=(?!\[\])", source, re.MULTILINE))
    assert source.count("complete_reasons(") >= responses, (
        f"{router} reports unreachable_nodes without complete_reasons, so a cluster it names can "
        "reach the banner with no reason"
    )
