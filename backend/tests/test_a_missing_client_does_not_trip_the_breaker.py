"""A cluster this worker has never connected to must not be marked failed.

`connection.py`'s `_auto_reconnect` already refuses to record a failure when it has no stored
connection info, and its comment says exactly why: an empty in-process cache is the normal
state for a cluster registered after the worker booted, and tripping the breaker there "seals
the only doors that could reopen it", because the Vault repair path is gated by the same
breaker.

Two callers defeated that one level up. Both ran `if health.get_kind(node) is None:
health.record_failure(...)` when `get_client` returned None. The guard reads as "preserve a
more specific diagnosis", but every path that actually reached the peer records its own kind
before returning None, so the guard fired only in the cases that must never trip: a cluster
this worker has not connected to yet, and one the caller's organization scope excluded. An
authorization decision and a cold worker both became a sixty second outage, across every
reader, with no log line anywhere.

Found through a browser suite that registered a cluster, proved it answering, and then watched
the same cluster read as unreachable seconds later while nothing in the log said why.
"""

import ast
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"

# Where a breaker trip is legitimate: the paths that actually tried to reach the peer.
ALLOWED = {
    "app/services/lxd/connection.py",
    "app/services/lxd/lxd_service.py",  # the auth-failure branch, checked by name below
}


def _record_failure_lines(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "record_failure":
            yield node.lineno, ast.unparse(node)


def test_no_caller_trips_the_breaker_merely_because_the_client_was_absent():
    """The literal that marked a cold worker as an outage must not come back."""
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, src in _record_failure_lines(tree):
            if "Client not connected" in src:
                offenders.append(f"{path.relative_to(APP.parent)}:{lineno}  {src}")

    assert not offenders, (
        "record_failure(..., 'Client not connected') trips the reachability breaker for a "
        "cluster that this worker simply has no client for, which connection.py refuses to do "
        "for the same reason. Report the node unreachable for that response instead. "
        "Offenders: " + "; ".join(offenders)
    )


def test_the_guard_can_see_the_pattern_it_bans():
    """Ablation, so a guard that stopped matching cannot pass silently."""
    tree = ast.parse('health.record_failure(node, "Client not connected")\n')
    found = [src for _, src in _record_failure_lines(tree) if "Client not connected" in src]
    assert found, "the matcher no longer recognises the banned call"

    clean = ast.parse('health.record_failure(node, f"connect failed: {type(e).__name__}")\n')
    assert not [
        src for _, src in _record_failure_lines(clean) if "Client not connected" in src
    ], "the matcher must not flag a genuine transport failure"


def test_a_real_transport_failure_still_records():
    """The breaker must keep working where it is earned: connection.py still records."""
    source = (APP / "services" / "lxd" / "connection.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    recorded = [src for _, src in _record_failure_lines(tree)]
    assert any("connect failed" in s for s in recorded), recorded
    assert any("reconnect failed" in s for s in recorded), recorded
