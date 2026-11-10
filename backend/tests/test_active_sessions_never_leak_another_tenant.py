"""Scoping a session list must fail closed.

get_active_sessions() reads user_email=None as "no user filter". AuthenticatedUser.email
is Optional, validate_jwt requires only exp/iss/sub, and email is read with a plain
payload.get, so a valid token carrying no email claim must never reach that filter with
None: it would be handed every session on every worker, across tenants.

Also pins scope=org isolation on the caller's own allowed-cluster set, matching how
/sessions/audit reads scope, so there is one authorization model.
"""

import time

from app.services.session_registry import get_session_registry
from app.services.session_tracker import SessionProtocol, SessionTracker, get_session_tracker
from tests.session_fakes import install_fake_registry


def _register(tracker, session_id, email, host="nuc2", instance=None):
    tracker.register(
        session_id=session_id,
        instance_name=instance or f"inst-{session_id}",
        host=host,
        project="default",
        protocol=SessionProtocol.TERMINAL,
        user_id="u1",
        user_email=email,
    )


async def test_a_user_sees_only_their_own_sessions():
    install_fake_registry()
    tracker = get_session_tracker()
    _register(tracker, "op-mine", "a@example.com")
    _register(tracker, "op-theirs", "b@example.com")

    sessions, _ = await tracker.list_active(user_email="a@example.com")

    assert [s.session_id for s in sessions] == ["op-mine"]


async def test_org_scope_excludes_a_cluster_the_caller_does_not_own():
    install_fake_registry()
    tracker = get_session_tracker()
    _register(tracker, "op-ours", "a@example.com", host="nuc2")
    _register(tracker, "op-foreign", "c@example.com", host="vdc-hexa")

    sessions, _ = await tracker.list_active(allowed_clusters={"nuc2"})

    assert [s.session_id for s in sessions] == ["op-ours"]


async def test_owning_no_clusters_matches_nothing_rather_than_everything():
    # The empty set is the dangerous case: read as falsy it becomes "no filter".
    install_fake_registry()
    tracker = get_session_tracker()
    _register(tracker, "op-ours", "a@example.com", host="nuc2")

    sessions, _ = await tracker.list_active(allowed_clusters={"__none__"})

    assert sessions == []


async def test_an_admin_is_unrestricted_by_cluster():
    install_fake_registry()
    tracker = get_session_tracker()
    _register(tracker, "op-a", "a@example.com", host="nuc2")
    _register(tracker, "op-b", "c@example.com", host="vdc-hexa")

    sessions, _ = await tracker.list_active(allowed_clusters=None)

    assert {s.session_id for s in sessions} == {"op-a", "op-b"}


async def test_isolation_holds_for_sessions_published_by_another_worker():
    # These rows were never in this worker's dict, so a filter applied only to local
    # sessions would pass them straight through.
    install_fake_registry()
    tracker = get_session_tracker()
    registry = get_session_registry()

    other = SessionTracker()
    _register(other, "op-foreign", "c@example.com", host="vdc-hexa")
    _register(other, "op-ours", "a@example.com", host="nuc2")
    await registry.publish(
        [s.to_snapshot("w2", time.time()) for s in other.live_sessions()], ttl_seconds=10
    )

    by_user, _ = await tracker.list_active(user_email="a@example.com")
    assert [s.session_id for s in by_user] == ["op-ours"]

    by_org, _ = await tracker.list_active(allowed_clusters={"nuc2"})
    assert [s.session_id for s in by_org] == ["op-ours"]


def test_the_session_endpoints_read_the_enforced_cluster_set():
    """
    Both session endpoints must scope on the request-scoped set, never recompute one.

    get_user_allowed_clusters() returns only the org-derived clusters.
    org_scope_dependency then narrows that by the tenant ACL and stores the result in the
    context var, and that is the set verify_cluster_access enforces. An endpoint calling the
    function directly skips the ACL layer, so a Tenant narrowed off a cluster its
    organisation owns would see the sessions there. The two calls look interchangeable at
    the call site, so this is checked structurally.
    """
    import ast
    from pathlib import Path

    console = Path(__file__).resolve().parents[1] / "app" / "routers" / "console.py"
    tree = ast.parse(console.read_text())

    offenders = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "get_user_allowed_clusters"
    ]
    assert not offenders, (
        f"console.py recomputes the allowed-cluster set at line(s) {offenders}; "
        "use get_current_allowed_clusters() so the ACL narrowing is not skipped"
    )
