"""
Regression guard for the RC3 production outage.

On 2026-07-30 a `requests` call with no timeout, running inline on the asyncio event loop,
parked all four uvicorn workers for 50 hours. The call was
`client.api.session.get(url, verify=False)` in `raw_proxy`.

Why a raw session call is special: pylxd only applies the client timeout when the call goes
through `client.api.<verb>`, because `_APINode.get` does
`kwargs["timeout"] = kwargs.get("timeout", self._timeout)`. Reaching for `client.api.session`
bypasses that, and a bare `requests.Session` has no default timeout, so the call blocks until
the kernel gives up. Against a peer that accepts TCP and then goes silent, that is never.

Every raw session or module-level `requests` call must therefore carry an explicit timeout.

This guard ships with NO allowlist on purpose. An allowlist is how this class of bug comes back.
"""

import ast
import unittest
from pathlib import Path

VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}
APP = Path(__file__).resolve().parents[1] / "app"


def _is_session(node: ast.expr) -> bool:
    """Receiver looks like a requests.Session (`session`, `self.session`, `client.api.session`)."""
    if isinstance(node, ast.Name):
        return node.id.endswith("session")
    if isinstance(node, ast.Attribute):
        return node.attr.endswith("session")
    return False


def _is_requests_module(node: ast.expr) -> bool:
    """Receiver is the `requests` module itself (`requests.get(...)`)."""
    return isinstance(node, ast.Name) and node.id == "requests"


def find_untimed_calls(root: Path) -> list[str]:
    """Return `path:line .verb()` for every raw HTTP call that passes no timeout."""
    offenders: list[str] = []
    for py in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), str(py))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute) or fn.attr not in VERBS:
                continue
            if not (_is_session(fn.value) or _is_requests_module(fn.value)):
                continue
            if any(kw.arg == "timeout" for kw in node.keywords):
                continue
            # note: a timeout smuggled through **kwargs would be a false positive here.
            # There are none today. If one appears, refactor the call rather than weaken
            # this guard.
            offenders.append(f"{py.relative_to(root.parent)}:{node.lineno} .{fn.attr}()")
    return offenders


class NoUntimedHttpCallsTest(unittest.TestCase):
    def test_every_raw_http_call_passes_a_timeout(self) -> None:
        offenders = find_untimed_calls(APP)
        self.assertEqual(
            [],
            offenders,
            "raw HTTP calls with no timeout (see module docstring):\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
