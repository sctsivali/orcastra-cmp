"""A backend storage fault must never be recorded against a cluster's reachability.

The circuit breaker in ``app.services.lxd.health`` decides whether a cluster is skipped on every
later request, and roughly every read path consults it. It is only ever allowed to answer a
question about the *peer*. When the backend's own tmpfs filled, ``mkstemp`` raised ``OSError(28)``
while preparing TLS credentials, a broad ``except Exception`` recorded it as ``KIND_OFFLINE``, and
operators were told every hypervisor was offline while all of them were healthy. The breaker then
suppressed for its whole TTL the retries that would have corrected the picture.

``LXDLocalStorageError`` exists to make that distinction catchable, but an exception type only
helps where somebody remembered to catch it first. Fixing the two connect sites was not enough:
``ensure_connection`` and ``check_node_status`` in lxd_service, and the reconnect in instance.py,
each re-recorded the same fault one layer up through their own broad handlers. Three separate
places had to agree, which is precisely the shape that rots the next time a handler is added.

So this is structural rather than behavioural. The rule: a handler catching ``Exception`` that
records a breaker failure unconditionally must first re-raise ``LXDLocalStorageError``. A record
guarded by an ``isinstance`` narrowing test is already type-scoped and is left alone.
"""

import ast
import unittest
from pathlib import Path
from typing import List, Tuple

APP = Path(__file__).resolve().parents[1] / "app"

BROAD = {"Exception", "BaseException"}
STORAGE = "LXDLocalStorageError"


def _is_record_failure(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "record_failure"
    return isinstance(func, ast.Name) and func.id == "record_failure"


def _caught_names(handler: ast.ExceptHandler) -> List[str]:
    if handler.type is None:
        return ["BaseException"]  # a bare except is as broad as it gets
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names = []
    for node in nodes:
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
    return names


def _records_unconditionally(handler: ast.ExceptHandler) -> bool:
    """True when the handler reaches record_failure without narrowing the type first."""
    for statement in handler.body:
        for node in ast.walk(statement):
            if not _is_record_failure(node):
                continue
            if _guarded_by_isinstance(handler, node):
                continue
            return True
    return False


def _guarded_by_isinstance(handler: ast.ExceptHandler, target: ast.Call) -> bool:
    for statement in handler.body:
        for node in ast.walk(statement):
            if not isinstance(node, ast.If):
                continue
            test_is_isinstance = any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "isinstance"
                for inner in ast.walk(node.test)
            )
            if not test_is_isinstance:
                continue
            if any(target is inner for branch in node.body for inner in ast.walk(branch)):
                return True
    return False


def _violations(tree: ast.AST) -> List[Tuple[int, str]]:
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        guards_storage = False
        for handler in node.handlers:
            names = _caught_names(handler)
            if STORAGE in names:
                guards_storage = True
                continue
            if not set(names) & BROAD:
                continue
            if _records_unconditionally(handler) and not guards_storage:
                found.append((handler.lineno, ", ".join(names)))
    return found


class StorageFaultsAreNotClusterFailures(unittest.TestCase):
    def test_no_broad_handler_records_a_breaker_failure_unguarded(self):
        offenders = []
        for path in sorted(APP.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for lineno, caught in _violations(tree):
                offenders.append(f"{path.relative_to(APP.parent)}:{lineno} catches {caught}")

        self.assertEqual(
            offenders, [],
            "these handlers record a breaker failure for any exception, including a backend "
            "storage fault the cluster had no part in. Add `except LXDLocalStorageError: raise` "
            "above the broad handler:\n  " + "\n  ".join(offenders),
        )

    def test_the_guard_detects_a_regression(self):
        """The checker must actually fail when the guard is removed, not pass vacuously."""
        regressed = ast.parse(
            "try:\n"
            "    connect()\n"
            "except Exception as e:\n"
            "    health.record_failure(cluster_id, str(e))\n"
        )
        self.assertEqual(len(_violations(regressed)), 1)

        repaired = ast.parse(
            "try:\n"
            "    connect()\n"
            "except LXDLocalStorageError:\n"
            "    raise\n"
            "except Exception as e:\n"
            "    health.record_failure(cluster_id, str(e))\n"
        )
        self.assertEqual(_violations(repaired), [])

    def test_an_isinstance_narrowed_record_is_not_flagged(self):
        """The raw proxy already type-scopes its record, and must not be forced to change."""
        narrowed = ast.parse(
            "try:\n"
            "    call()\n"
            "except Exception as api_error:\n"
            "    if isinstance(api_error, (requests.exceptions.ConnectionError,)):\n"
            "        health.record_failure(cluster_id, str(api_error))\n"
        )
        self.assertEqual(_violations(narrowed), [])


if __name__ == "__main__":
    unittest.main()
