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
import pathlib
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


class SingleFlightDecoratorTests(unittest.TestCase):
    """The decorator must stay invisible to FastAPI's parameter resolution.

    FastAPI builds query parameters, dependencies and the response model from
    inspect.signature of the registered callable. A wrapper that loses the signature
    silently turns every parameter into an unfilled positional argument, and the failure
    surfaces as a 500 at request time rather than at import, so a structural test is the
    only cheap guard.
    """

    def test_signature_survives_the_wrapper(self):
        import asyncio
        import inspect

        from app.core.single_flight import single_flight_on_fresh

        @single_flight_on_fresh("probe")
        async def handler(host: str = "nuc2", fresh: bool = False) -> str:
            return f"{host}:{fresh}"

        params = inspect.signature(handler).parameters
        self.assertIn("host", params, "the wrapper hid a query parameter from FastAPI")
        self.assertIn("fresh", params)
        self.assertEqual(params["fresh"].default, False)

        # and it must still be callable both ways
        self.assertEqual(asyncio.run(handler(host="a", fresh=False)), "a:False")
        self.assertEqual(asyncio.run(handler(host="a", fresh=True)), "a:True")

    def test_a_plain_read_never_takes_the_lock(self):
        import asyncio

        from app.core import single_flight

        @single_flight.single_flight_on_fresh("probe")
        async def handler(fresh: bool = False) -> str:
            return "ok"

        before = dict(single_flight._locks)
        asyncio.run(handler(fresh=False))
        self.assertEqual(
            single_flight._locks, before, "a cached read must not serialise behind a rebuild"
        )

    def test_every_decorated_handler_is_registered_below_the_route(self):
        """The decorator must sit under @router.get, or FastAPI registers the undecorated
        function and the serialisation never runs."""
        offenders = []
        for path in sorted(ROUTERS.glob("*.py")):
            lines = path.read_text().split("\n")
            for i, line in enumerate(lines):
                if not line.startswith("@single_flight_on_fresh"):
                    continue
                after = next((l for l in lines[i + 1:] if l.strip()), "")
                if not after.startswith(("async def", "def")):
                    offenders.append(f"{path.name}:{i + 1} is not directly above its handler")
                before = next((l for l in reversed(lines[:i]) if l.strip()), "")
                if before.strip() not in (")", "@router.get"):
                    offenders.append(f"{path.name}:{i + 1} is not under a route decorator")
        self.assertEqual(offenders, [])


class SharedBypassCounterTests(unittest.TestCase):
    """The shared counter must limit when Redis answers and must not block when it does not.

    A rate limiter that fails closed converts a Redis outage into a total product outage.
    That is strictly worse than the thing it protects against, so the fallback direction is
    the property worth pinning.
    """

    def _with_redis(self, client):
        import app.middleware.security as security

        class _Manager:
            redis_client = client

        return security, _Manager()

    def test_it_limits_once_the_budget_is_spent(self):
        counts = {}

        class FakeRedis:
            def incr(self, key):
                counts[key] = counts.get(key, 0) + 1
                return counts[key]

            def expire(self, key, seconds):
                return True

        security, manager = self._with_redis(FakeRedis())
        import app.core.cache as cache_mod

        original = cache_mod.get_cache_manager
        cache_mod.get_cache_manager = lambda: manager
        try:
            verdicts = [security._shared_allow("fresh:alice", 3, 60) for _ in range(5)]
        finally:
            cache_mod.get_cache_manager = original

        self.assertEqual(verdicts, [True, True, True, False, False])

    def test_two_subjects_do_not_share_a_counter(self):
        counts = {}

        class FakeRedis:
            def incr(self, key):
                counts[key] = counts.get(key, 0) + 1
                return counts[key]

            def expire(self, key, seconds):
                return True

        security, manager = self._with_redis(FakeRedis())
        import app.core.cache as cache_mod

        original = cache_mod.get_cache_manager
        cache_mod.get_cache_manager = lambda: manager
        try:
            for _ in range(3):
                security._shared_allow("fresh:alice", 3, 60)
            bob = security._shared_allow("fresh:bob", 3, 60)
        finally:
            cache_mod.get_cache_manager = original

        self.assertTrue(bob, "alice spending her budget must not limit bob")

    def test_it_falls_back_rather_than_blocking_when_redis_is_down(self):
        class BrokenRedis:
            def incr(self, key):
                raise ConnectionError("redis is down")

        security, manager = self._with_redis(BrokenRedis())
        import app.core.cache as cache_mod

        original = cache_mod.get_cache_manager
        cache_mod.get_cache_manager = lambda: manager
        try:
            verdict = security._shared_allow("fresh:alice", 3, 60)
        finally:
            cache_mod.get_cache_manager = original

        self.assertIsNone(
            verdict,
            "a store failure must return None so the caller falls back, never False",
        )

    def test_it_falls_back_when_redis_is_absent(self):
        security, manager = self._with_redis(None)
        import app.core.cache as cache_mod

        original = cache_mod.get_cache_manager
        cache_mod.get_cache_manager = lambda: manager
        try:
            self.assertIsNone(security._shared_allow("fresh:alice", 3, 60))
        finally:
            cache_mod.get_cache_manager = original


class FailureIsLabelledTests(unittest.TestCase):
    """A failed host must be labelled, not merely excluded from the cache.

    The earlier guard only proved a degraded fan-out was not cached. That passed while the
    guard was inert: images.py caught its own per-node failure and returned a valid empty
    response, so the "did anyone answer" counter never reached zero and the false empty was
    cached anyway. Structural presence is not behaviour. This asserts the label instead.
    """

    PER_HOST = {
        "images.py": "ImageListResponse",
        "projects.py": "ProjectListResponse",
    }

    def test_per_host_failures_construct_an_unreachable_entry(self):
        offenders = []
        for filename, cls in self.PER_HOST.items():
            tree = ast.parse((ROUTERS / filename).read_text())
            constructions = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == cls
            ]
            labelled = [
                call
                for call in constructions
                if any(kw.arg == "reachable" for kw in call.keywords)
            ]
            if not labelled:
                offenders.append(f"{filename}: no {cls} is ever constructed with reachable=")
            # every construction inside an exception handler must carry the label
            for handler in [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]:
                for call in ast.walk(handler):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == cls
                        and not any(kw.arg == "reachable" for kw in call.keywords)
                    ):
                        offenders.append(
                            f"{filename}:{call.lineno} builds {cls} in an except block "
                            "without saying the host was unreachable"
                        )
        self.assertEqual(offenders, [])

    def test_flattening_handlers_report_which_nodes_were_lost(self):
        """Category B returns one flat list, so the only way to name a failed node is a
        top-level field. A handler that drops the name reports an outage as an empty list."""
        expected = {
            "profiles.py", "network_acls.py", "lxd_operations.py", "lxd_warnings.py",
            "storage_pools.py", "storage_volumes.py", "permissions.py", "ipam.py",
            "lxd_server.py",
        }
        offenders = []
        for filename in sorted(expected):
            source = (ROUTERS / filename).read_text()
            if "unreachable_nodes" not in source:
                offenders.append(f"{filename}: no unreachable_nodes anywhere")
                continue
            if "unreachable_nodes.append(" not in source:
                offenders.append(f"{filename}: never records a failed node")
            if "unreachable_nodes=unreachable_nodes" not in source:
                offenders.append(f"{filename}: records failures but never returns them")
        self.assertEqual(offenders, [])


class ServiceLayerMustNotSwallowUnreachableTests(unittest.TestCase):
    """A service method that returns empty when it cannot connect blinds every caller.

    This is the bug class that survived two fixes. The router was taught to label an
    unreachable host, and the label never appeared, because one layer down
    `list_images` did `if client is None: return []`. The router saw a successful empty
    result and had nothing to label. Against the real fleet, fourteen dead clusters all
    reported reachable=True with zero images.

    The rule is narrow and checkable: inside the LXD service layer, discovering that
    there is no client is a failure and must be raised. Callers can decide how to present
    it; they cannot invent information the service threw away.
    """

    SERVICES = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "lxd"

    EXEMPT = {
        # Returns (items, connected) where the second element IS the reachability signal,
        # which is how instances and VMs report connected_nodes to their routers.
        "_fetch_instances_for_node",
    }

    def test_no_service_method_returns_empty_when_it_cannot_connect(self):
        offenders = []
        for path in sorted(self.SERVICES.glob("*.py")):
            tree = ast.parse(path.read_text())
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if fn.name in self.EXEMPT:
                    continue
                for node in ast.walk(fn):
                    # `if client is None:` and variants
                    if not isinstance(node, ast.If):
                        continue
                    test = node.test
                    is_none_check = (
                        isinstance(test, ast.Compare)
                        and isinstance(test.ops[0], ast.Is)
                        and isinstance(test.comparators[0], ast.Constant)
                        and test.comparators[0].value is None
                        and isinstance(test.left, ast.Name)
                        and "client" in test.left.id
                    )
                    if not is_none_check:
                        continue
                    for stmt in node.body:
                        if not isinstance(stmt, ast.Return):
                            continue
                        value = stmt.value
                        empty = (
                            value is None
                            or (isinstance(value, ast.List) and not value.elts)
                            or (isinstance(value, ast.Dict) and not value.keys)
                            or (isinstance(value, ast.Constant) and value.value in (None, ""))
                        )
                        if empty:
                            offenders.append(
                                f"{path.name}:{stmt.lineno} {fn.name} returns empty when it "
                                "cannot connect, so its caller cannot tell an outage from a "
                                "genuinely empty result"
                            )
        self.assertEqual(offenders, [])
