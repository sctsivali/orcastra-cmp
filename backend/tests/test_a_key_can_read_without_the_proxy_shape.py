"""An issued API key could authenticate and then reach almost nothing readable.

Measured against the running deployment with a real key: `GET /instances`, `/nodes`, `/projects`
and even `/integrations/available-clusters` all answered 401, because those are the JWT routers.
The only readable endpoint in the entire API was `/integrations/regions`. Reading instances meant
`POST /integrations/raw-proxy` with a body naming an HTTP verb and a `cluster_id` the key had no
way to discover, and `/docs` is disabled outside debug, so there was no reference to read either.

A 401 on the obvious call, with no documentation, reads as "this needs a login". That is what was
reported, and it is a correct reading of what the API did.

These reads add no reach: each is the data `POST /integrations/sync` already returns to the same
caller, through the same fetch helpers, narrowed by the same intersection with the key's grant.
What changes is the shape. `whoami` is separate: `/validate` answers it already but takes the
credentials in the BODY, so an integrator sending the headers every other endpoint requires gets
a 422 and concludes the key is broken.
"""

import ast
import inspect
from pathlib import Path

import pytest

from app.routers import integrations

ROUTER = Path(inspect.getfile(integrations))

READS = ("list_integration_clusters", "list_integration_projects", "list_integration_instances")


def _key(*cluster_ids: str) -> dict:
    return {
        "id": "oak_test",
        "app_name": "test",
        "cluster_permissions": [
            {"cluster_id": c, "permissions": ["read"], "projects": ["default"]}
            for c in cluster_ids
        ],
    }


# ── scoping ────────────────────────────────────────────────────────────────────────────────────

def test_a_read_is_confined_to_the_keys_own_clusters() -> None:
    assert integrations._clusters_for_key(_key("a", "b"), None) == {"a", "b"}


def test_a_caller_cannot_name_a_cluster_outside_its_grant() -> None:
    """Intersection, not a lookup. Naming a foreign cluster yields nothing, not an error.

    An unknown cluster and an unauthorised one are therefore indistinguishable, so neither can be
    probed, which is the same shape assert_cluster_permission already chose.
    """
    assert integrations._clusters_for_key(_key("a", "b"), "victim") == set()


def test_a_caller_can_narrow_to_one_of_its_own() -> None:
    assert integrations._clusters_for_key(_key("a", "b"), "b") == {"b"}


def test_a_key_with_no_grant_reads_nothing() -> None:
    assert integrations._clusters_for_key(_key(), None) == set()
    assert integrations._clusters_for_key(_key(), "a") == set()


def test_a_malformed_grant_entry_is_skipped_rather_than_dereferenced() -> None:
    key = {"cluster_permissions": [{"cluster_id": "a"}, "not-a-dict", {"no_cluster": 1}]}
    assert integrations._clusters_for_key(key, None) == {"a"}


# ── the reads are gated, and reuse rather than reinvent ────────────────────────────────────────

@pytest.mark.parametrize("name", READS)
def test_a_read_requires_inventory_read(name: str) -> None:
    parameters = inspect.signature(getattr(integrations, name)).parameters
    default = parameters["api_key"].default
    rendered = repr(getattr(default, "dependency", default))
    assert "require_inventory_read" in rendered, f"{name} is not capability gated: {rendered}"


@pytest.mark.parametrize("name", READS)
def test_a_read_scopes_through_the_shared_helper(name: str) -> None:
    """Reinventing the intersection per endpoint is how one of them ends up subtly wider."""
    source = inspect.getsource(getattr(integrations, name))
    assert "_clusters_for_key(api_key, cluster_id)" in source


@pytest.mark.parametrize("name", READS)
def test_a_read_reuses_the_fetch_helper_sync_already_uses(name: str) -> None:
    source = inspect.getsource(getattr(integrations, name))
    assert "_gather_per_cluster" in source, (
        f"{name} fetches its own way; the shared helper is what skips a tripped cluster and "
        f"keeps the fan-out off the event loop"
    )


# ── whoami ─────────────────────────────────────────────────────────────────────────────────────

def test_whoami_authenticates_with_the_headers_everything_else_uses() -> None:
    """The point of it. /validate takes a body, which is why a header-only caller gets a 422."""
    parameters = inspect.signature(integrations.whoami).parameters
    assert "request" not in parameters, "whoami must not take a body"
    default = parameters["api_key"].default
    assert getattr(default, "dependency", None) is integrations.validate_api_key


def test_whoami_reports_only_what_the_presented_key_already_holds() -> None:
    """It is exempt from the capability rule, so it must not become a way to learn anything new."""
    source = inspect.getsource(integrations.whoami)
    tree = ast.parse(source.lstrip())
    reads = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    for forbidden in ("get_api_key_from_vault", "list_api_keys_from_vault", "db.execute"):
        assert not any(forbidden in call for call in reads), (
            f"whoami reaches for {forbidden}; it must answer purely from the validated key"
        )


def test_whoami_is_a_get_so_it_can_be_a_one_line_curl() -> None:
    source = ROUTER.read_text()
    marker = '@external_router.get(\n    "/whoami",'
    assert marker in source


# ── the collection reads are GETs, which is the whole complaint ────────────────────────────────

@pytest.mark.parametrize("path", ["/clusters", "/projects", "/instances", "/whoami"])
def test_the_new_reads_are_gets(path: str) -> None:
    source = ROUTER.read_text()
    assert f'@external_router.get(\n    "{path}",' in source, (
        f"{path} is not a GET; the reported problem was that reading required a POST whose body "
        f"named an HTTP verb"
    )
