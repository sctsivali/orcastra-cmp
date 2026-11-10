"""A backend storage fault must never be recorded against a cluster's reachability.

The breaker in ``app.services.lxd.health`` makes later requests skip a cluster, so it may
record only the peer's faults. A full tmpfs makes ``mkstemp`` raise ``OSError(28)`` while TLS
credentials are prepared; a broad handler that records it marks every hypervisor offline for
the breaker's TTL. The connect sites each have such a handler, so the rule is structural: a
broad handler that records a failure unconditionally needs an earlier
``except LXDLocalStorageError: raise`` in the same ``try``. A record behind an ``isinstance``
test is already type-scoped and is left alone.
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
        """The checker flags a broad handler without the guard and passes once it is added."""
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
        """A record narrowed by isinstance, as in the raw proxy, is already type-scoped."""
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
