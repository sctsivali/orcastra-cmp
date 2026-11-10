"""A failed reconnect must not become a 500 because two threads cleaned up at once.

``_temp_cert_paths`` is a plain dict with no lock, and ``get_client`` runs inside a thread
pool via ``run_blocking``. A test-then-delete cleanup lets two threads both pass the
membership test, and the loser raises ``KeyError``. The cleanup runs inside
``_auto_reconnect``'s failure handler, so that KeyError would replace the connection error
and turn an unreachable cluster into a 500.

The deterministic tests force the interleaving; the threaded one is closer to the real
shape but can only fail probabilistically, so it supplements them.
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
        """The loser of the race returns quietly instead of raising."""
        mgr = LXDConnectionManager()
        cert_path, key_path = _seed(mgr, "bliss01")

        # The winner completes first.
        mgr._cleanup_temp_cert_files("bliss01")
        self.assertNotIn("bliss01", mgr._temp_cert_paths)
        self.assertFalse(os.path.exists(cert_path))
        self.assertFalse(os.path.exists(key_path))

        # The loser arrives after the entry is gone.
        try:
            mgr._cleanup_temp_cert_files("bliss01")
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
        _seed(mgr, "panda-east")

        real_exists = os.path.exists
        removed = threading.Event()

        def exists_then_steal(path):
            # The other thread finishes its own cleanup right here.
            if not removed.is_set():
                removed.set()
                mgr._temp_cert_paths.pop("panda-east", None)
            return real_exists(path)

        os.path.exists = exists_then_steal
        try:
            mgr._cleanup_temp_cert_files("panda-east")
        except KeyError as exc:
            self.fail(f"cleanup raised KeyError({exc}) when the entry was stolen mid-body")
        finally:
            os.path.exists = real_exists

        self.assertTrue(removed.is_set(), "the interleaving was never exercised")

    def test_many_threads_cleaning_one_cluster_all_survive(self):
        """Concurrent cleanups of one cluster all survive (probabilistic)."""
        mgr = LXDConnectionManager()
        _seed(mgr, "nuc-2")

        errors = []

        def clean():
            try:
                mgr._cleanup_temp_cert_files("nuc-2")
            except Exception as exc:  # noqa: BLE001 - any exception fails the test
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for future in [pool.submit(clean) for _ in range(32)]:
                future.result()

        self.assertEqual(errors, [], f"concurrent cleanup raised: {errors}")
        self.assertNotIn("nuc-2", mgr._temp_cert_paths)


if __name__ == "__main__":
    unittest.main()
