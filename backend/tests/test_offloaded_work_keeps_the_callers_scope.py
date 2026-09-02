"""Work moved off the event loop must carry the caller's organization scope with it.

`loop.run_in_executor` does not copy contextvars; `asyncio.to_thread` does, because it wraps
the call in `ctx.run` itself. This codebase used both, so half the offloaded work saw the
caller's scope and half saw the defaults, and nothing said which was which.

Three things read a ContextVar and answer permissively when it is unset, which is what makes
the difference matter rather than merely being untidy:

  org_scope._allowed_clusters_var   None means "no filter", so the org-scope branch at the top
                                    of connection.get_client could never deny anything reached
                                    through a worker thread.
  org_scope.get_scope_cache_key     returns ":scope=all" on a None var, which is the ADMIN
                                    cache namespace, so a scoped read computed in a worker
                                    would write into it.
  org_scope.check_tenant_permission returns True on a None ACL. In a worker that is
                                    unconditional: it fails OPEN.

Only the first was reachable in practice, and only as a second line of defence behind
`verify_cluster_access`. The other two are one careless offload away, which is why this is a
behavioural test rather than a comment.
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
        """The two submit branches used to be separate; only one of them can be tested by luck."""

        async def go():
            _probe.set({"cluster-b"})
            return await run_blocking(
                _read_kwargs, timeout=5, label="probe", executor=_executor, marker=7
            )

        self.assertEqual(({"cluster-b"}, 7), asyncio.run(go()))

    def test_the_raw_executor_still_loses_it(self):
        """The ablation. Without this the test above passes for the wrong reason: a fresh
        thread would read the same default if the var were never set."""

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
        """The behavioural case, against the real var rather than a stand-in."""
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
        """The trap in the fix: `Context.run` refuses re-entry.

        One `copy_context()` shared by N concurrent offloads raises "cannot enter context"
        rather than losing the scope quietly, so the copy must be per call. run_blocking copies
        inside itself, which is what makes ten simultaneous cluster reads safe; routers that
        build their own task list have to copy inside the comprehension for the same reason.
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
        """Ablation: prove the failure mode is real, so the per-call copy is not cargo cult."""

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
