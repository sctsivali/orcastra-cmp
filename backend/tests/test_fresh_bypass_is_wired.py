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


CONNECTION_ERROR = "LXDConnectionError"


def _tries_swallowing_a_connection_error(source: str):
    """Yield the lineno of every `try` that raises the connection error while a broad
    handler catches it and no passthrough lets it out.

    The rule lives here rather than in one test class because it is the same rule on both
    sides of the layer boundary. It was written for app/services/lxd/ and the identical
    defect then shipped in six app/routers/ helpers, where nothing was looking: the router
    check next to it was a substring scan, and both strings were present because the inner
    per-item handler supplied the second one.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        raises_conn = any(
            isinstance(n, ast.Raise)
            and n.exc is not None
            and getattr(n.exc.func if isinstance(n.exc, ast.Call) else n.exc, "id", None)
            == CONNECTION_ERROR
            for stmt in node.body
            for n in ast.walk(stmt)
        )
        if not raises_conn:
            continue
        names = [
            getattr(h.type, "id", getattr(h.type, "attr", "bare")) if h.type else "bare"
            for h in node.handlers
        ]
        if "Exception" not in names and "bare" not in names:
            continue
        passthrough = any(
            getattr(h.type, "id", None) == CONNECTION_ERROR
            and isinstance(h.body[-1], ast.Raise)
            and h.body[-1].exc is None
            for h in node.handlers
        )
        if not passthrough:
            yield node.lineno


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

    def test_every_handler_reporting_unreachable_guards_its_cache_write(self):
        """The scan above only sees handlers that call list_all_vms.

        Nine wrapper routers fan out through run_in_executor instead, so they were invisible
        to it and every one of them cached a total outage: /networks, /profiles, /ipam,
        /storage/volumes and both storage-pool resource endpoints pinned an empty result for
        120 to 180 seconds. This scan discovers by the property that actually matters, which
        is that the handler reports unreachable_nodes at all, so a new fan-out is covered the
        day it is written.
        """
        offenders, checked = [], 0
        for path, fn in self._get_handlers():
            if fn.name in self.FAN_OUT_EXEMPT:
                continue
            reports = any(
                kw.arg == "unreachable_nodes"
                for node in ast.walk(fn)
                if isinstance(node, ast.Call)
                for kw in node.keywords
            )
            if not reports:
                continue
            sets = [c for c in self._calls(fn, "set", "cache") if _writes_response_cache(c)]
            if not sets:
                continue
            checked += 1

            # any `if <name>:` will do. The guard names differ by handler (answered,
            # connected_nodes), and pinning the vocabulary would only invite a rename.
            guard_names = {
                node.test.id
                for node in ast.walk(fn)
                if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
            }
            guarded = self._guarded_call_ids(fn, guard_names)
            offenders += [
                f"{path.name}:{c.lineno} {fn.name} caches unguarded"
                for c in sets
                if id(c) not in guarded
            ]

        # 7 at the time of writing: instances, vms, networks, profiles, ipam,
        # storage_pools, storage_volumes. Six more report unreachable_nodes without caching
        # at all, so the rule does not apply to them.
        self.assertGreaterEqual(checked, 7, "the scan stopped finding reporting handlers")
        self.assertEqual(
            offenders,
            [],
            "these handlers report unreachable_nodes but cache the result unconditionally, "
            f"so a fleet-wide outage is pinned for the whole TTL: {offenders}",
        )

    def test_storage_pool_resources_does_not_cache_an_unanswered_fleet(self):
        """The two resource endpoints return a bare dict with no unreachable_nodes field,
        so the scan above cannot see them. They are the worst case of the class: the value
        is `{"resources": {}}`, which renders as every pool having zero capacity."""
        tree = ast.parse((ROUTERS / "storage_pools.py").read_text())
        names = {
            "get_all_storage_pool_resources",
            "get_all_storage_pool_resources_legacy_path",
        }
        found = [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
        ]
        self.assertEqual(len(found), 2, "both resource handlers must exist; update this test")
        unguarded = []
        for fn in found:
            sets = [c for c in self._calls(fn, "set", "cache") if _writes_response_cache(c)]
            self.assertTrue(sets, f"{fn.name} no longer caches; update this test")
            guarded = self._guarded_call_ids(fn, {"answered"})
            unguarded += [f"storage_pools.py:{c.lineno}" for c in sets if id(c) not in guarded]
        self.assertEqual(
            unguarded,
            [],
            f"pool resources cached without checking any node answered: {unguarded}",
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

    def test_every_spelling_the_handler_accepts_is_budgeted(self):
        """The middleware and the handler must agree on what "fresh" means.

        `fresh: bool = False` is parsed by pydantic, which coerces t, T, y and Y as well
        as the obvious spellings. The middleware matched a hand-written set that had none
        of them, so `?fresh=t` bypassed the response cache and yielded no `fresh:` bucket,
        leaving a forced fan-out counted only against the general limit. Both sides are now
        the same parser, and this pins that they stay so.
        """
        from pydantic import TypeAdapter, ValidationError

        flag = TypeAdapter(bool)
        candidates = [
            "1", "true", "TRUE", "True", "t", "T", "y", "Y", "yes", "on",
            "0", "false", "f", "n", "no", "off", "2", "", "maybe",
        ]
        offenders = []
        for value in candidates:
            try:
                handler_sees = flag.validate_python(value)
            except ValidationError:
                handler_sees = False
            budgeted = bool(
                [k for k, _ in self._keys(f"fresh={value}") if k.startswith("fresh:")]
            )
            if handler_sees != budgeted:
                offenders.append(
                    f"fresh={value!r}: handler bypasses={handler_sees}, budgeted={budgeted}"
                )
        self.assertEqual(offenders, [])

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

    def test_a_leader_that_raised_is_not_recorded_as_a_completed_rebuild(self):
        """A failed rebuild must not hand the next arrival a licence to read the cache.

        mark_rebuilt used to sit in the finally, so a leader that answered 503 still
        recorded a finished rebuild. The waiter behind it then took the fresh=False branch
        and served the very cache it had asked to bypass, which on these endpoints is kept
        warm by the metrics collector. Two operators clicking refresh at the same moment
        during an outage got different answers, one a 503 and one a stale 200.

        The follower has to arrive before the leader fails, which is the whole point:
        sequentially the second request's arrival timestamp is already newer than the
        mark, so it does the work either way and the defect is invisible.
        """
        import asyncio

        from app.core import single_flight

        seen = []

        @single_flight.single_flight_on_fresh("probe_fail")
        async def handler(host: str = "nuc2", fresh: bool = False) -> str:
            seen.append(fresh)
            await asyncio.sleep(0.02)
            if fresh:
                raise RuntimeError("cluster unreachable")
            return "stale body from the cache the caller asked to skip"

        async def drive():
            return await asyncio.gather(
                *(handler(host="nuc2", fresh=True) for _ in range(2)),
                return_exceptions=True,
            )

        results = asyncio.run(drive())
        self.assertTrue(
            all(isinstance(r, RuntimeError) for r in results),
            f"a follower was served the cache while the leader failed: {results}",
        )
        self.assertEqual(
            seen,
            [True, True],
            "the follower took the fresh=False branch on the strength of a rebuild that "
            f"never completed: {seen}",
        )
        self.assertEqual(single_flight._locks, {}, "the lock bookkeeping leaked")

    def test_concurrent_forced_reads_still_collapse_into_one_rebuild(self):
        """The counterpart: fixing the failure path must not break the success path."""
        import asyncio

        from app.core import single_flight

        seen = []

        @single_flight.single_flight_on_fresh("probe_chain")
        async def handler(fresh: bool = False) -> str:
            seen.append(fresh)
            await asyncio.sleep(0.02)
            return "built" if fresh else "cached"

        async def drive():
            return await asyncio.gather(*(handler(fresh=True) for _ in range(3)))

        results = asyncio.run(drive())
        self.assertEqual(len(results), 3)
        # exactly one leader rebuilt; the other two read what it produced
        self.assertEqual(seen.count(True), 1, f"expected one rebuild, got {seen}")
        self.assertEqual(seen.count(False), 2)

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
            # a node can also be absent because it was never asked: these handlers query
            # only nodes with a live client, so a dead cluster is skipped rather than failed
            if "set(lxd_service.get_node_names()) - set(nodes_to_query)" not in source:
                offenders.append(
                    f"{filename}: reports nodes that failed but not nodes it never queried, "
                    "so a dead cluster goes missing instead of being named"
                )
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
        # note: this used to name "_fetch_instances_for_node", which has never existed under
        # app/. An exemption for a function that does not exist relaxes nothing and hides
        # that it is relaxing nothing, which is why test_every_exemption_names_a_real_function
        # now guards the set itself.
        "_fetch_vms_from_node",
    }

    # (file, function, variable) where a None client genuinely is not a connection failure.
    TYPED_EXEMPT = {
        # The cluster was reached successfully a few lines above; this is a per-project
        # client that could not be opened, which is an operation failure, not an outage.
        ("instance.py", "create_instance", "create_client"),
    }

    CONNECTION_ERROR = "LXDConnectionError"

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
                            # a tuple of empties blinds the caller exactly as much
                            or (
                                isinstance(value, ast.Tuple)
                                and value.elts
                                and all(
                                    isinstance(e, (ast.List, ast.Dict)) and not (
                                        getattr(e, "elts", None) or getattr(e, "keys", None)
                                    )
                                    or (isinstance(e, ast.Constant) and e.value in (None, ""))
                                    for e in value.elts
                                )
                            )
                        )
                        if empty:
                            offenders.append(
                                f"{path.name}:{stmt.lineno} {fn.name} returns empty when it "
                                "cannot connect, so its caller cannot tell an outage from a "
                                "genuinely empty result"
                            )
        self.assertEqual(offenders, [])

    def test_it_raises_a_connection_error_not_an_operation_error(self):
        """Raising the wrong type is as broken as returning empty, and looks correct.

        LXDConnectionError and LXDOperationError are siblings off Exception
        (exceptions.py), so `except LXDOperationError` never catches a connection failure
        and vice versa. Routers map the two to different statuses: connection to 503,
        operation to 400, 404 or 500 depending on the handler.

        Raising the operation type from a `client is None` branch is what produced 14 x
        HTTP 500 on GET /api/v1/projects/blitz01, and a 404 claiming an instance did not
        exist on a cluster that was merely offline.
        """
        offenders = []
        for path in sorted(self.SERVICES.glob("*.py")):
            # availability.py is the boundary translator: raising HTTPException(503) from a
            # missing client is precisely its job, and it is not called by other services.
            if path.name == "availability.py":
                continue
            tree = ast.parse(path.read_text())
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if not isinstance(node, ast.If):
                        continue
                    test = node.test
                    if not (
                        isinstance(test, ast.Compare)
                        and isinstance(test.ops[0], ast.Is)
                        and isinstance(test.comparators[0], ast.Constant)
                        and test.comparators[0].value is None
                        and isinstance(test.left, ast.Name)
                        and "client" in test.left.id
                    ):
                        continue
                    if (path.name, fn.name, test.left.id) in self.TYPED_EXEMPT:
                        continue
                    for stmt in node.body:
                        if not isinstance(stmt, ast.Raise) or stmt.exc is None:
                            continue
                        exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
                        name = getattr(exc, "id", getattr(exc, "attr", None))
                        if name != self.CONNECTION_ERROR:
                            offenders.append(
                                f"{path.name}:{stmt.lineno} {fn.name} raises {name} when it "
                                f"cannot connect. Only {self.CONNECTION_ERROR} is mapped to "
                                "503; every other type becomes a 400, 404 or 500."
                            )
        self.assertEqual(offenders, [])

    def test_a_connection_error_is_not_rewrapped_by_a_broad_handler(self):
        """Raising the right type is only half the fix.

        projects.py raised inside a try whose `except Exception` re-wrapped it as
        LXDOperationError, so changing the type alone accomplished nothing at all. Any try
        that raises the connection type while also catching Exception needs the
        passthrough, as images.py has.
        """
        offenders = [
            f"{path.name}:{lineno} raises {self.CONNECTION_ERROR} inside a try whose broad "
            "handler re-wraps it. Add `except LXDConnectionError: raise` above it, as "
            "images.py does."
            for path in sorted(self.SERVICES.glob("*.py"))
            for lineno in _tries_swallowing_a_connection_error(path.read_text())
        ]
        self.assertEqual(offenders, [])

    def test_every_exemption_names_a_real_function(self):
        """An exemption for a function that does not exist is worse than no exemption.

        It relaxes nothing while reading as though it does, so nobody re-examines it. This
        set spent an unknown period exempting `_fetch_instances_for_node`, which has never
        existed anywhere under app/.
        """
        defined = set()
        for path in self.SERVICES.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defined.add(node.name)
        missing = sorted(self.EXEMPT - defined)
        self.assertEqual(
            missing, [], f"exemptions naming functions that do not exist: {missing}"
        )
        stale = sorted(
            f"{f}:{fn}" for f, fn, _ in self.TYPED_EXEMPT if fn not in defined
        )
        self.assertEqual(stale, [], f"typed exemptions naming functions that do not exist: {stale}")



class UnreachableIsNotTheSameAsErroringTests(unittest.TestCase):
    """A node that erred mid-listing must not be reported as a node that is down.

    Reproduced live: deleting a network while another request listed them raised
    "Network not found", the helper treated any exception as unreachability, and a healthy
    node came back with zero networks and a banner saying it could not be reached. The
    forced-refresh path made this easy to hit, because it lists for real on every click
    instead of serving cache.

    So the two conditions get two answers. No client means unreachable and must raise, and
    must pass through the generic handler untouched. Anything else that goes wrong while
    listing is partial data: log it, return what was collected, do not condemn the node.
    """

    HELPERS = [
        "profiles.py", "network_acls.py", "lxd_operations.py", "lxd_warnings.py",
        "storage_pools.py", "storage_volumes.py", "permissions.py", "ipam.py",
        "lxd_server.py",
    ]

    def test_a_missing_client_raises(self):
        offenders = [
            f"{filename}: a missing client does not raise, so the caller cannot tell the "
            "node was unreachable"
            for filename in self.HELPERS
            if "raise LXDConnectionError" not in (ROUTERS / filename).read_text()
        ]
        self.assertEqual(offenders, [])

    def test_the_raise_is_not_swallowed_by_the_helper_s_own_broad_handler(self):
        """Presence of both strings is not the property; reaching the caller is.

        Six of these helpers raised the right type and caught it in an inner per-item
        handler, while their own outer `except Exception` turned the raise straight back
        into an empty list. Every substring the previous version of this test looked for
        was present the whole time, so a breaker-tripped cluster reported "this node has
        nothing" on six endpoints and was never named in unreachable_nodes.
        """
        offenders = [
            f"{filename}:{lineno} raises {CONNECTION_ERROR} inside a try whose own broad "
            "handler swallows it, so the node is reported empty rather than unreachable. "
            "Add `except LXDConnectionError: raise` above it, as profiles.py does."
            for filename in self.HELPERS
            for lineno in _tries_swallowing_a_connection_error((ROUTERS / filename).read_text())
        ]
        self.assertEqual(offenders, [])

    def test_the_scan_actually_covers_something(self):
        """A scan whose helper list has drifted out of the tree passes for the wrong reason."""
        missing = [f for f in self.HELPERS if not (ROUTERS / f).exists()]
        self.assertEqual(missing, [])
        self.assertGreaterEqual(len(self.HELPERS), 9)

    def test_a_listing_error_does_not_re_raise(self):
        """The generic handler must not re-raise: that is what turned a transient item
        failure into 'this whole node is down'."""
        offenders = []
        for filename in self.HELPERS + ["networks.py"]:
            tree = ast.parse((ROUTERS / filename).read_text())
            for handler in [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]:
                # only the broad handler; a named LXDConnectionError passthrough is correct
                name = getattr(handler.type, "id", None)
                if name != "Exception":
                    continue
                for stmt in handler.body:
                    if isinstance(stmt, ast.Raise) and stmt.exc is None:
                        offenders.append(
                            f"{filename}:{stmt.lineno} re-raises any listing error, which "
                            "reports a healthy node as unreachable"
                        )
        self.assertEqual(offenders, [])


class NeverQueriedNodesAreNamedTests(unittest.TestCase):
    """A fan-out asks connection_manager.get_node_names(), which is only the nodes holding a
    live client. Everything Vault has registered beyond that is never queried at all, so a
    handler that fills unreachable_nodes from failed tasks alone reports an empty fleet as a
    healthy one. networks.py shipped without this seeding while nine sibling routers had it,
    and no test could see the difference because each router was only ever read on its own."""

    # two idioms subtract from the same scoped fleet: the wrappers subtract the nodes they
    # asked, instances/vms subtract the nodes that answered. Match the shared prefix so a
    # router with no seeding at all still fails.
    SEED = "set(lxd_service.get_node_names()) - set("

    def _routers_reporting_unreachable(self):
        return sorted(
            path.name
            for path in ROUTERS.glob("*.py")
            if "unreachable_nodes" in path.read_text()
        )

    def test_every_router_that_reports_unreachable_seeds_the_never_queried(self):
        offenders = [
            filename
            for filename in self._routers_reporting_unreachable()
            if self.SEED not in (ROUTERS / filename).read_text()
        ]
        self.assertEqual(
            offenders,
            [],
            "these routers name only the nodes that failed, so a cluster with no live client "
            "is reported as absent rather than unreachable",
        )

    def test_the_scan_actually_covers_something(self):
        """A scan over an empty set passes for the wrong reason."""
        self.assertGreaterEqual(len(self._routers_reporting_unreachable()), 10)
        self.assertIn("networks.py", self._routers_reporting_unreachable())


class CachedPayloadsWrittenBeforeTheFieldStillParseTests(unittest.TestCase):
    """Every list handler reconstructs its model from a cached dict. A payload written by the
    previous build has none of the reachability fields, so making one of them required would
    raise ValidationError on every cached read until each TTL expired, which on a rolling
    deploy is an outage rather than a stale banner. Most of these wrappers are declared inside
    their router rather than in schemas.py, so this scans both."""

    # "error" alone is too broad: ErrorResponse.error is a required message and has nothing to
    # do with reachability, so it is only checked on a model that also carries "reachable".
    ALWAYS = ("unreachable_nodes", "reachable")

    def _declarations(self):
        """Yield (where, field, source_line) for every reachability field declaration."""
        sources = [("schemas.py", (ROUTERS.parent / "models" / "schemas.py").read_text())]
        sources += [(path.name, path.read_text()) for path in sorted(ROUTERS.glob("*.py"))]
        for where, source in sources:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                declared = {
                    stmt.target.id: stmt
                    for stmt in node.body
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                }
                names = [n for n in self.ALWAYS if n in declared]
                if "reachable" in declared and "error" in declared:
                    names.append("error")
                for field in names:
                    yield f"{where}:{node.name}.{field}", declared[field]

    def test_no_reachability_field_is_required(self):
        offenders = []
        checked = 0
        for where, stmt in self._declarations():
            checked += 1
            if stmt.value is None:
                offenders.append(f"{where} (bare annotation)")
                continue
            call = stmt.value
            if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "Field":
                positional_default = bool(call.args) and not (
                    isinstance(call.args[0], ast.Constant) and call.args[0].value is Ellipsis
                )
                keyword_default = any(
                    kw.arg in ("default", "default_factory") for kw in call.keywords
                )
                if not (positional_default or keyword_default):
                    offenders.append(f"{where} (Field(...) with no default)")
        self.assertEqual(
            offenders,
            [],
            "a cached payload written before this field existed would fail to parse",
        )
        # a scan over nothing passes for the wrong reason
        self.assertGreaterEqual(checked, 12)


class PrewarmMustFillWhatTheEndpointFillsTests(unittest.TestCase):
    """The prewarm writes the payload the endpoint later serves, so any field the endpoint sets
    must be set here too. Leaving unreachable_nodes to its model default wrote an empty list into
    the cache, and every unforced read reported a healthy fleet while fourteen clusters were down.
    The forced path was correct throughout, which is why the endpoint tests all passed. The same
    shape mismatch previously made GET /nodes answer 500 for a whole TTL."""

    def _models_declaring(self, field):
        source = (ROUTERS.parent / "models" / "schemas.py").read_text()
        tree = ast.parse(source)
        return {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and any(
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id == field
                for stmt in node.body
            )
        }

    def test_prewarm_passes_unreachable_nodes_to_every_model_that_has_it(self):
        collector = ROUTERS.parent / "services" / "metrics_collector.py"
        models = self._models_declaring("unreachable_nodes")
        self.assertIn("VMListResponse", models, "fixture is wrong, not the code")

        offenders = []
        constructed = 0
        for node in ast.walk(ast.parse(collector.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None)
            if name not in models:
                continue
            constructed += 1
            if not any(kw.arg == "unreachable_nodes" for kw in node.keywords):
                offenders.append(f"{name} at metrics_collector.py:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "a prewarm builds this model without unreachable_nodes, so the cached payload "
            "reports a healthy fleet and the banner never appears on an unforced read",
        )
        # a scan over nothing passes for the wrong reason
        self.assertGreaterEqual(constructed, 2)

    def test_a_prewarm_refuses_to_write_a_fan_out_nobody_answered(self):
        """The endpoint guard is worthless if the collector reinstates the outage.

        instances.py and vms.py both refuse to cache a fan-out where no cluster answered.
        The collector writes those exact keys 30s later and had no such refusal, so the
        guard closed the front door while the prewarm walked in the back. The signal is a
        fetcher returning None, which the caller's existing `if data:` already honours.
        """
        collector = ROUTERS.parent / "services" / "metrics_collector.py"
        tree = ast.parse(collector.read_text())
        models = self._models_declaring("unreachable_nodes")

        offenders, checked = [], 0
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            builds = any(
                getattr(n.func, "id", None) in models
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
            )
            if not builds:
                continue
            checked += 1
            bails = any(
                isinstance(n, ast.Return)
                and isinstance(n.value, ast.Constant)
                and n.value.value is None
                for n in ast.walk(fn)
            )
            if not bails:
                offenders.append(f"{fn.name} at metrics_collector.py:{fn.lineno}")
        self.assertEqual(
            offenders,
            [],
            "these prewarm fetchers always return a payload, so an outage is written into "
            "the same key the endpoint deliberately left cold",
        )
        self.assertGreaterEqual(checked, 2)

    def test_every_prewarm_states_the_ttl_its_endpoint_uses(self):
        """A single hardcoded TTL in the helper is a second, invisible cache policy.

        It was 180 for all four, against endpoints using 30, 180, 120 and 60, so the
        instances list survived six times longer than GET /instances intends. The TTL is
        the endpoint's decision and belongs at the call site.
        """
        collector = ROUTERS.parent / "services" / "metrics_collector.py"
        tree = ast.parse(collector.read_text())

        helper = next(
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_prewarm_single_cache"
        )
        self.assertIn(
            "ttl",
            [a.arg for a in helper.args.args],
            "_prewarm_single_cache must take the TTL rather than choosing one",
        )
        self.assertEqual(
            helper.args.defaults, [], "a default TTL here silently reintroduces the bug"
        )
        for call in ast.walk(helper):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "set"
            ):
                ttl = next((kw.value for kw in call.keywords if kw.arg == "ttl"), None)
                self.assertIsInstance(
                    ttl, ast.Name, "the helper must pass its ttl argument through, not a literal"
                )

        # (cache-key prefix -> the ttl its handler uses). Sources, all cache.set calls:
        # routers/instances.py list_instances, routers/vms.py list_vms,
        # routers/regions.py get_regions_overview, routers/storage_pools.py resources-all.
        expected = {
            "instances:list": 30,
            "vms:list": 180,
            "regions:overview": 120,
            "storage_pool_resources:all": 60,
        }
        seen = {}
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and getattr(call.func, "attr", None) == "_prewarm_single_cache"):
                continue
            key_arg = call.args[1] if len(call.args) > 1 else None
            if not (isinstance(key_arg, ast.Call) and key_arg.args):
                continue
            key = key_arg.args[0].value
            ttl = next((kw.value.value for kw in call.keywords if kw.arg == "ttl"), None)
            seen[next((p for p in expected if key.startswith(p)), key)] = ttl
        self.assertEqual(seen, expected)
