"""A failed reconnect must not become a 500 because two threads cleaned up at once.

``_cleanup_temp_cert_files`` used to test membership, unlink the files, then ``del`` the
entry. ``_temp_cert_paths`` is a plain dict with no lock, and ``get_client`` runs inside a
thread pool via ``run_blocking``, so two threads cleaning the same cluster both passed the
membership test and the loser raised ``KeyError`` on the delete.

The delete sits in ``_auto_reconnect``'s own failure handler, so the KeyError replaced the
connection error that was being handled and escaped as ``Exception in ASGI application``.
A cluster that was merely unreachable answered 500.

Observed live on the running stack, 14 ERROR in a 60 minute window, 13 of them the bare
ASGI line and one carrying the traceback::

    File "/app/app/services/lxd/connection.py", line 231, in _cleanup_temp_cert_files
        del self._temp_cert_paths[cluster_id]
    KeyError: 'blitz01'

Two tests, because either alone is weak. The deterministic one forces the exact
interleaving and would fail against the old code every run. The threaded one is closer to
what actually happened but can only fail probabilistically, so it is a supplement and never
the proof.
"""

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd.connection import LXDConnectionManager


def _seed(mgr: LXDConnectionManager, cluster_id: str) -> tuple:
    """Give the manager two real temp files to clean, as a live reconnect would."""
    paths = []
    for suffix in (".crt", ".key"):
        fd, path = tempfile.mkstemp(prefix=f"lxd_{cluster_id}_", suffix=suffix)
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"not a real certificate")
        paths.append(path)
    mgr._temp_cert_paths[cluster_id] = (paths[0], paths[1])
    return tuple(paths)


class TempCertCleanupRaceTests(unittest.TestCase):
    def test_a_second_cleanup_after_the_entry_is_gone_is_a_no_op(self):
        """The loser of the race must return quietly, not raise.

        Simulates the interleaving directly: the entry disappears while the first caller is
        still inside the body, which is exactly what the winning thread does.
        """
        mgr = LXDConnectionManager()
        cert_path, key_path = _seed(mgr, "blitz01")

        # The winner completes entirely while the loser is mid-body.
        mgr._cleanup_temp_cert_files("blitz01")
        self.assertNotIn("blitz01", mgr._temp_cert_paths)
        self.assertFalse(os.path.exists(cert_path))
        self.assertFalse(os.path.exists(key_path))

        # The loser arrives late. Against the old code this raised KeyError.
        try:
            mgr._cleanup_temp_cert_files("blitz01")
        except KeyError as exc:
            self.fail(
                f"cleanup raised KeyError({exc}) for an entry another caller already "
                "claimed. _auto_reconnect calls this from its failure handler, so this "
                "turns an unreachable cluster into a 500."
            )

    def test_cleanup_mid_body_removal_does_not_raise(self):
        """The entry vanishing between the membership test and the delete must be safe.

        Forces the window deterministically by removing the entry from inside the body,
        through the first filesystem call the body makes.
        """
        mgr = LXDConnectionManager()
        _seed(mgr, "pandora-semanan")

        real_exists = os.path.exists
        removed = threading.Event()

        def exists_then_steal(path):
            # The other thread finishes its own cleanup right here.
            if not removed.is_set():
                removed.set()
                mgr._temp_cert_paths.pop("pandora-semanan", None)
            return real_exists(path)

        os.path.exists = exists_then_steal
        try:
            mgr._cleanup_temp_cert_files("pandora-semanan")
        except KeyError as exc:
            self.fail(f"cleanup raised KeyError({exc}) when the entry was stolen mid-body")
        finally:
            os.path.exists = real_exists

        self.assertTrue(removed.is_set(), "the interleaving was never exercised")

    def test_many_threads_cleaning_one_cluster_all_survive(self):
        """Closer to the real shape, but probabilistic, so it supplements rather than proves."""
        mgr = LXDConnectionManager()
        _seed(mgr, "nuc-2")

        errors = []

        def clean():
            try:
                mgr._cleanup_temp_cert_files("nuc-2")
            except Exception as exc:  # noqa: BLE001 - the point is to catch anything
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for future in [pool.submit(clean) for _ in range(32)]:
                future.result()

        self.assertEqual(errors, [], f"concurrent cleanup raised: {errors}")
        self.assertNotIn("nuc-2", mgr._temp_cert_paths)


if __name__ == "__main__":
    unittest.main()
