"""A cluster this worker has never connected to must not be marked failed.

`connection.py`'s `_auto_reconnect` refuses to record a failure when it has no stored
connection info: an empty in-process cache is the normal state for a cluster registered after
the worker booted, and tripping the breaker there would also block the Vault repair path,
which the same breaker gates.

A caller must not undo that one level up by recording a failure whenever `get_client` returns
None. Every path that reached the peer records its own kind before returning None, so such a
call fires only for a cluster this worker has not connected to yet or one the caller's
organization scope excluded, and turns either into a sixty second outage for every reader.
"""

import ast
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"

# Where a breaker trip is legitimate: the paths that tried to reach the peer.
ALLOWED = {
    "app/services/lxd/connection.py",
    "app/services/lxd/lxd_service.py",  # the auth-failure branch
}


def _record_failure_lines(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "record_failure":
            yield node.lineno, ast.unparse(node)


def test_no_caller_trips_the_breaker_merely_because_the_client_was_absent():
    """No caller records "Client not connected": that marks a cold worker as an outage."""
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
