"""Ratchet: a GET that caches its response must offer a way to bypass that cache.

The refresh controls in the console call `refetch()`, which re-issues the identical URL. If
the handler serves that URL from the Redis response cache (TTLs here run 30s to 3600s), the
operator gets the same bytes back and the button looks broken. It was exactly that: `fresh`
existed on two endpoints, and no UI path reached either of them.

The bypass belongs only to explicit user actions. Background polling keeps reading the cache
on purpose, because the endpoints worth refreshing are the ones that fan out across every
cluster, and letting every open tab bypass would turn routine polling into a fan-out storm.

This is structural rather than a request test because the failure is silent: the endpoint
still returns 200 with a plausible body, and the operator simply does not see their own
change. A new cached GET is caught automatically, so there is no list here to forget to
update.
"""

import ast
import unittest
from pathlib import Path

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"

# Namespaces that hold whole cached responses. A read from one of these inside a GET handler
# is the thing that makes a refresh return stale data.
RESPONSE_CACHES = {"api_responses", "lxd_resources"}

EXEMPT = {
    # Reads monitoring_history only, which is an append-only series rather than a cached
    # response. Its own dashboard data comes from get_monitoring_dashboard, which does take
    # fresh, and the history points are identical whether or not you bypass anything.
    "get_metrics_history",
}


def _is_router_get(node: ast.AST) -> bool:
    for dec in getattr(node, "decorator_list", []):
        func = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(func, ast.Attribute) and func.attr == "get":
            if isinstance(func.value, ast.Name) and func.value.id == "router":
                return True
    return False


def _reads_response_cache(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "cache"):
            continue
        if sub.args and isinstance(sub.args[0], ast.Constant):
            if sub.args[0].value in RESPONSE_CACHES:
                return True
    return False


def _writes_response_cache(call: ast.Call) -> bool:
    """True for cache.set into a namespace holding a whole cached response."""
    return bool(call.args) and isinstance(call.args[0], ast.Constant) and call.args[0].value in RESPONSE_CACHES


def _dashboard_cache_writes(fn: ast.AST) -> list:
    """The monitoring dashboard keeps its own in-process response cache, as a dict."""
    return [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Subscript)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "_dashboard_cache"
    ]


def _arg_names(node: ast.AST) -> set:
    a = node.args
    return {arg.arg for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs)}


class FreshBypassTests(unittest.TestCase):
    def test_cached_get_handlers_accept_fresh(self):
        offenders = []
        checked = 0
        for path in sorted(ROUTERS.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not _is_router_get(node) or not _reads_response_cache(node):
                    continue
                if node.name in EXEMPT:
                    continue
                checked += 1
                if "fresh" not in _arg_names(node):
                    offenders.append(f"{path.name}:{node.lineno} {node.name}")

        self.assertGreater(checked, 15, "the scan stopped finding cached GET handlers")
        self.assertEqual(
            offenders,
            [],
            "these GET handlers cache their response but offer no bypass, so their "
            f"refresh button returns cached data: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()


class DegradedFanOutTests(unittest.TestCase):
    """A fan-out where no cluster answered must not be remembered as the answer.

    Confirmed live during a QA run: GET /images returned HTTP 200 with total=0 for three
    clusters including one the write path reports as not connected. The connection breaker
    short-circuits before any network call, so the read path cannot tell "nothing there"
    from "nobody answered". Caching that pins it for the whole TTL, and a cache bypass makes
    it operator-triggerable: refresh during an outage and good data on screen is replaced by
    the outage's empty result.

    On a monitoring surface it is worse than on a list. An empty inventory looks broken, so
    someone investigates. "0 CPU, 0 traffic, 0 instances" looks like a healthy idle cluster.

    The scan discovers handlers rather than naming them, because a ratchet that covers a
    subset reads as full coverage to the next person. Anything it genuinely cannot infer is
    named in EXEMPT with the reason it is safe, or asserted explicitly below.
    """

    # Handlers whose degraded path is real but not expressible as "guard the cache write".
    FAN_OUT_EXEMPT = {
        # Returns early with is_online=False for a disconnected node, before reaching any
        # cache.set. The `_` discard is the instance list, not the reachability signal:
        # reachability comes from node.connected, checked at routers/monitoring.py:861.
        "get_node_metrics",
    }

    def _get_handlers(self):
        for path in sorted(ROUTERS.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_router_get(node):
                    yield path, node

    @staticmethod
    def _calls(node, attr, value_id=None):
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if isinstance(f, ast.Attribute) and f.attr == attr:
                if value_id is None or (isinstance(f.value, ast.Name) and f.value.id == value_id):
                    yield sub

    @staticmethod
    def _guarded_call_ids(fn, guard_names):
        """ids of Call nodes sitting inside `if <name>:` for any name in guard_names."""
        out = set()
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Name)
                and node.test.id in guard_names
            ):
                out |= {id(c) for c in ast.walk(node) if isinstance(c, (ast.Call, ast.Assign))}
        return out

    def test_fan_out_handlers_do_not_cache_an_unanswered_result(self):
        offenders, checked = [], 0
        for path, fn in self._get_handlers():
            if fn.name in self.FAN_OUT_EXEMPT:
                continue
            if not any(self._calls(fn, "list_all_vms")):
                continue
            sets = [c for c in self._calls(fn, "set", "cache") if _writes_response_cache(c)]
            sets += _dashboard_cache_writes(fn)
            if not sets:
                continue
            checked += 1

            # Names that could carry the connected-nodes half of a fan-out result.
            # note: collected from every 2-tuple unpack in the handler, not only from a
            # direct list_all_vms() assignment, because the binding is often one hop away:
            # `(all_vms, connected_nodes), statuses = await asyncio.gather(task, ...)`.
            # A `_` discard contributes no name, which is the case worth catching.
            guard_names = set()
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assign):
                    continue
                for t in ast.walk(node.targets[0]):
                    if isinstance(t, ast.Tuple) and len(t.elts) == 2:
                        second = t.elts[1]
                        if isinstance(second, ast.Name) and second.id != "_":
                            guard_names.add(second.id)

            if not guard_names:
                offenders.append(
                    f"{path.name}:{fn.lineno} {fn.name} discards the connected-nodes signal"
                )
                continue

            guarded = self._guarded_call_ids(fn, guard_names)
            for call in sets:
                if id(call) not in guarded:
                    offenders.append(f"{path.name}:{call.lineno} {fn.name} caches unguarded")

        self.assertGreaterEqual(checked, 3, "the scan stopped finding fan-out handlers")
        self.assertEqual(
            offenders,
            [],
            "these handlers cache a fan-out result without checking that any cluster "
            f"answered, so an outage gets cached as healthy emptiness: {offenders}",
        )

    def test_image_listing_does_not_cache_a_fleet_that_never_answered(self):
        """images.py fans out per node rather than through list_all_vms, so it is asserted
        directly: an exception per node is rendered as that node having zero images."""
        tree = ast.parse((ROUTERS / "images.py").read_text())
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "list_all_images"
        )
        sets = list(self._calls(fn, "set", "cache"))
        self.assertTrue(sets, "list_all_images no longer caches; update this test")
        guarded = self._guarded_call_ids(fn, {"answered"})
        unguarded = [f"images.py:{c.lineno}" for c in sets if id(c) not in guarded]
        self.assertEqual(
            unguarded,
            [],
            f"image list cached without checking any node answered: {unguarded}",
        )


class BypassBudgetTests(unittest.TestCase):
    """The bypass needs its own budget, and it cannot be keyed on the client IP.

    Every browser reaches the API through the Next.js rewrite proxy, so the socket
    address the middleware sees is one container for the whole tenancy. Observed in the
    access log: 429s attributed to a single client ip across a dozen paths and several
    users. An IP-keyed budget for a forced fan-out would therefore let one operator's
    refreshes throttle everyone else, which is a worse failure than the one it prevents.
    """

    def _keys(self, query: str, auth: str | None = None):
        from starlette.requests import Request

        from app.middleware.security import RequestSecurityMiddleware

        headers = [(b"authorization", auth.encode())] if auth else []
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/instances",
            "query_string": query.encode(),
            "headers": headers,
        }
        mw = RequestSecurityMiddleware.__new__(RequestSecurityMiddleware)
        from app.core.config import get_settings

        mw.settings = get_settings()
        return list(mw._rate_limit_keys("10.0.0.1", Request(scope)))

    def test_a_plain_read_gets_no_bypass_bucket(self):
        keys = [k for k, _ in self._keys("host=nuc2")]
        self.assertFalse([k for k in keys if k.startswith("fresh:")])

    def test_a_forced_read_gets_its_own_smaller_bucket(self):
        entries = self._keys("host=nuc2&fresh=true")
        fresh = [(k, limit) for k, limit in entries if k.startswith("fresh:")]
        self.assertEqual(len(fresh), 1, "a bypass must be budgeted separately")
        _, limit = fresh[0]
        general = next(lim for k, lim in entries if k.startswith("ip:"))
        self.assertLess(limit, general, "the bypass budget must be smaller than the general one")

    def test_two_operators_behind_one_proxy_do_not_share_a_bypass_bucket(self):
        # unsigned tokens: the middleware reads the claim to pick a counter, never to authorise
        import base64
        import json as _json

        def token(sub):
            def seg(d):
                return base64.urlsafe_b64encode(_json.dumps(d).encode()).rstrip(b"=").decode()

            return f"Bearer {seg({'alg': 'none'})}.{seg({'sub': sub})}.x"

        a = [k for k, _ in self._keys("fresh=true", token("alice@example.com")) if k.startswith("fresh:")]
        b = [k for k, _ in self._keys("fresh=true", token("bob@example.com")) if k.startswith("fresh:")]
        self.assertNotEqual(a, b, "same proxy IP, different operators, must not share a budget")
