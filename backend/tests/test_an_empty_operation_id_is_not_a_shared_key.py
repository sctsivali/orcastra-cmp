"""An unusable operation id must never become a key other sessions share.

Session ids used to be whatever LXD put in metadata.id, read with a default of "".
While each worker had its own dict that was merely useless - one bucket, one short-lived
broken session. As a field in a hash shared by four workers it is corrupting: every
session that got an empty id collapses onto the same entry, and the first one to end
deletes the others' snapshots out from under them.

The console's old fallback was worse and also dead. `f"console-{name}-{id(self)}"` keys
on a CPython address, which is process-local and reused after garbage collection, so it
can collide across workers and with a disconnected session still awaiting cleanup. It
never ran either, because operation_id is always PRESENT - just sometimes empty - so a
.get() default could not fire. The guard has to be on falsiness.
"""

import ast
from pathlib import Path

from app.services.session_models import new_session_id

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"


def test_a_real_operation_id_is_used_as_is():
    assert new_session_id("b1b0c0de-1234", "terminal") == "b1b0c0de-1234"


def test_an_empty_or_blank_id_becomes_a_unique_one():
    for unusable in ("", "   ", None):
        first = new_session_id(unusable, "console")
        second = new_session_id(unusable, "console")
        assert first.startswith("console-")
        assert first != second, "two sessions must not share a generated id"


def test_neither_router_keys_a_session_on_a_memory_address():
    # id(self) is not an identity: it is reused after collection and means nothing in
    # another process. Structural, because the failure is invisible at runtime until two
    # sessions happen to land on the same address. Matched on the AST rather than the
    # text, so the comment explaining this defect does not trip its own guard.
    for name in ("terminal.py", "console.py"):
        tree = ast.parse((ROUTERS / name).read_text())
        offenders = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "id"
        ]
        assert not offenders, f"{name} still keys on a memory address"


def test_both_routers_derive_the_session_id_through_the_guard():
    # the guard only helps where it is actually used, and this is the kind of call that
    # gets quietly inlined again during a later edit.
    for name in ("terminal.py", "console.py"):
        tree = ast.parse((ROUTERS / name).read_text())
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "new_session_id"
        ]
        assert calls, f"{name} does not derive its session id through new_session_id()"
