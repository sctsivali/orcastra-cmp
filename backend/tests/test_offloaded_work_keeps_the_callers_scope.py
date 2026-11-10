"""Work moved off the event loop must carry the caller's organization scope with it.

`loop.run_in_executor` does not copy contextvars; `asyncio.to_thread` does, because it wraps
the call in `ctx.run` itself. Three readers answer permissively when their ContextVar is unset:

  org_scope._allowed_clusters_var   None means "no filter", so the org-scope branch at the top
                                    of connection.get_client denies nothing in a worker thread.
  org_scope.get_scope_cache_key     returns ":scope=all" on a None var, the Admin cache
                                    namespace, so a scoped read computed in a worker writes there.
  org_scope.check_tenant_permission returns True on a None ACL: in a worker it fails open.

`run_blocking` copies the caller's context for each call, and these tests hold it to that.
"""

import asyncio
import contextvars
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from app.core.blocking import run_blocking


_probe: contextvars.ContextVar = contextvars.ContextVar("probe", default=None)
_executor = ThreadPoolExecutor(max_workers=2)


def _read():
    return _probe.get()


def _read_kwargs(*, marker):
    return (_probe.get(), marker)


class OffloadedScopeTests(unittest.TestCase):
    def test_a_contextvar_survives_the_hop(self):
        async def go():
            _probe.set({"cluster-a"})
            return await run_blocking(
                _read, timeout=5, label="probe", executor=_executor
            )

        self.assertEqual({"cluster-a"}, asyncio.run(go()))

    def test_it_survives_the_kwargs_path_too(self):
        """run_blocking submits a call with kwargs on its own branch, which carries it too."""

        async def go():
            _probe.set({"cluster-b"})
            return await run_blocking(
                _read_kwargs, timeout=5, label="probe", executor=_executor, marker=7
            )

        self.assertEqual(({"cluster-b"}, 7), asyncio.run(go()))

    def test_the_raw_executor_still_loses_it(self):
        """Control case: a raw executor loses the var, so the tests above prove run_blocking
        carries it rather than reading a default."""

        async def go():
            _probe.set({"cluster-c"})
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(_executor, _read)

        self.assertIsNone(asyncio.run(go()))

    def test_an_unset_var_still_reads_as_unset(self):
        """Copying a context must not invent a value. A background sweep has no scope and must
        keep having none, or the filter starts denying the healer its own work."""

        async def go():
            return await run_blocking(
                _read, timeout=5, label="probe", executor=_executor
            )

        self.assertIsNone(asyncio.run(go()))

    def test_the_org_scope_var_specifically_survives(self):
        """The real org-scope var survives the hop as well as the stand-in."""
        from app.core import org_scope

        async def go():
            org_scope._allowed_clusters_var.set({"only-this-one"})
            return await run_blocking(
                org_scope.get_current_allowed_clusters,
                timeout=5,
                label="scope",
                executor=_executor,
            )

        self.assertEqual({"only-this-one"}, asyncio.run(go()))


    def test_many_calls_at_once_each_get_their_own_context(self):
        """`Context.run` refuses re-entry, so each concurrent offload needs its own copy.

        One `copy_context()` shared by N concurrent offloads raises "cannot enter context".
        run_blocking copies per call; a router that builds its own task list must copy inside
        the comprehension for the same reason.
        """

        from app.core import org_scope

        async def scenario():
            async def one(value):
                token = org_scope._allowed_clusters_var.set({value})
                try:
                    return await run_blocking(
                        org_scope.get_current_allowed_clusters,
                        timeout=5,
                        label="concurrent",
                        executor=_executor,
                    )
                finally:
                    org_scope._allowed_clusters_var.reset(token)

            return await asyncio.gather(*(one(f"c{i}") for i in range(12)))

        results = asyncio.run(scenario())
        self.assertEqual([{f"c{i}"} for i in range(12)], results)

    def test_one_shared_context_is_the_bug_this_avoids(self):
        """Control case: a shared Context does raise, which is why the copy is per call."""

        async def scenario():
            loop = asyncio.get_running_loop()
            shared = contextvars.copy_context()

            def slow():
                time.sleep(0.05)
                return True

            return await asyncio.gather(
                *(
                    loop.run_in_executor(_executor, partial(shared.run, slow))
                    for _ in range(4)
                ),
                return_exceptions=True,
            )

        outcomes = asyncio.run(scenario())
        self.assertTrue(
            any(isinstance(o, RuntimeError) for o in outcomes),
            "expected 'cannot enter context' from a shared Context; if this stops failing, "
            "the per-call copy can be simplified",
        )

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
