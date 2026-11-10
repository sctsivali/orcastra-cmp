"""An unusable operation id must never become a key other sessions share.

LXD's metadata.id can be empty, and session state is a hash shared by every worker, so
sessions with an empty id would collapse onto one entry and the first to end would delete
the others' snapshots. operation_id is always PRESENT, sometimes empty, so a .get() default
never fires: the guard has to test falsiness. Keying on `id(self)` is no fallback either: a
CPython address is process-local and reused after garbage collection, so it can collide
across workers and with a disconnected session still awaiting cleanup.
"""

import ast
from pathlib import Path

from app.services.session_models import new_session_id

SERVICES = Path(__file__).resolve().parents[1] / "app" / "services"
# The routers hand the socket to these; the session id is derived where the operation is created.
PROXIES = (SERVICES / "terminal_proxy.py", SERVICES / "console_proxy.py")


def test_a_real_operation_id_is_used_as_is():
    assert new_session_id("b1b0c0de-1234", "terminal") == "b1b0c0de-1234"


def test_an_empty_or_blank_id_becomes_a_unique_one():
    for unusable in ("", "   ", None):
        first = new_session_id(unusable, "console")
        second = new_session_id(unusable, "console")
        assert first.startswith("console-")
        assert first != second, "two sessions must not share a generated id"


def test_neither_proxy_keys_a_session_on_a_memory_address():
    # id(self) is reused after collection and means nothing in another process, and a
    # collision stays invisible until two sessions land on one address. Matched on the AST
    # rather than the text, so a comment that mentions id(self) does not trip the guard.
    for path in PROXIES:
        tree = ast.parse(path.read_text())
        name = path.name
        offenders = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "id"
        ]
        assert not offenders, f"{name} still keys on a memory address"


def test_both_proxies_derive_the_session_id_through_the_guard():
    # The guard only helps where it is used, and a later edit could inline the derivation
    # again.
    for path in PROXIES:
        tree = ast.parse(path.read_text())
        name = path.name
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "new_session_id"
        ]
        assert calls, f"{name} does not derive its session id through new_session_id()"
