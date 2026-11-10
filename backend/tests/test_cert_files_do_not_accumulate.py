"""LXD certificate files on the backend tmpfs must be bounded, whatever the concurrency.

``get_client`` runs in a thread pool via ``run_blocking``, so several threads can connect to the
same cluster at once, and a cluster that never connects is never cached in ``_clients``, so every
request re-enters ``_auto_reconnect`` and writes its certificates again. Each cluster therefore has
one fixed pair of paths, replaced atomically, so concurrent writers cannot orphan files.

A full tmpfs is a local storage fault (``LXDLocalStorageError``). Raised as ``LXDConnectionError``
it would be recorded against the circuit breaker, report every healthy cluster as offline and
suppress the retries that would show otherwise.
"""

import errno
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd import cert_files, health
from app.services.lxd.connection import LXDConnectionManager
from app.services.lxd.exceptions import LXDConnectionError, LXDLocalStorageError

CERT_PEM = "-----BEGIN CERTIFICATE-----\nnot a real certificate\n-----END CERTIFICATE-----\n"
KEY_PEM = "-----BEGIN PRIVATE KEY-----\nnot a real key\n-----END PRIVATE KEY-----\n"


class CertFileIsolation(unittest.TestCase):
    """Point the certificate base at a scratch directory so the suite never touches the real /tmp."""

    def setUp(self):
        self._previous = os.environ.get(cert_files.CERT_BASE_ENV)
        self._scratch = tempfile.mkdtemp(prefix="orcastra-cert-tests-")
        os.environ[cert_files.CERT_BASE_ENV] = self._scratch
        self.addCleanup(self._restore)

    def _restore(self):
        if self._previous is None:
            os.environ.pop(cert_files.CERT_BASE_ENV, None)
        else:
            os.environ[cert_files.CERT_BASE_ENV] = self._previous
        for root, dirs, files in os.walk(self._scratch, topdown=False):
            for name in files:
                os.unlink(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(self._scratch)

    def _cert_files_on_disk(self):
        root = cert_files.cert_root()
        if not os.path.isdir(root):
            return []
        return sorted(os.listdir(root))


class MaterializeIsBounded(CertFileIsolation):
    def test_sixty_four_concurrent_writers_leave_exactly_one_pair(self):
        """64 concurrent writers for one cluster leave exactly one pair."""
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = [
                future.result()
                for future in [
                    pool.submit(cert_files.materialize, "bliss01", CERT_PEM, KEY_PEM)
                    for _ in range(64)
                ]
            ]

        on_disk = self._cert_files_on_disk()
        self.assertEqual(
            len(on_disk), 2,
            f"64 concurrent connects to one cluster left {len(on_disk)} files: {on_disk}. "
            "Each orphaned pair is unreachable for cleanup and fills the 256 MiB tmpfs.",
        )
        self.assertEqual(len(set(results)), 1, "concurrent callers disagreed on the path")

    def test_every_writer_sees_complete_content(self):
        """Writes go through os.replace, so no reader observes a half-written certificate."""
        with ThreadPoolExecutor(max_workers=16) as pool:
            for future in [
                pool.submit(cert_files.materialize, "nuc-2", CERT_PEM, KEY_PEM)
                for _ in range(32)
            ]:
                cert_path, key_path = future.result()
                with open(cert_path) as handle:
                    self.assertEqual(handle.read(), CERT_PEM)
                with open(key_path) as handle:
                    self.assertEqual(handle.read(), KEY_PEM)

    def test_repeated_clusters_do_not_each_add_a_pair(self):
        """Sixteen clusters connecting ten times each leave sixteen pairs."""
        clusters = [f"my-cluster-{index:02d}" for index in range(16)]
        for _ in range(10):
            for cluster_id in clusters:
                cert_files.materialize(cluster_id, CERT_PEM, KEY_PEM)

        self.assertEqual(len(self._cert_files_on_disk()), 2 * len(clusters))

    def test_the_path_is_a_pure_function_of_the_cluster_id(self):
        first = cert_files.paths_for("panda-east")
        second = cert_files.paths_for("panda-east")
        self.assertEqual(first, second)
        self.assertNotEqual(first, cert_files.paths_for("panda"))

    def test_permissions_stay_owner_only(self):
        cert_path, key_path = cert_files.materialize("center1-alpha", CERT_PEM, KEY_PEM)
        for path in (cert_path, key_path):
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, path)

    def test_a_hostile_cluster_id_cannot_escape_the_cert_directory(self):
        """Cluster ids arrive from the registration API, so this is a traversal boundary."""
        for hostile in ("../../etc/passwd", "..", "/etc/shadow", "a/b/../../../../root/.ssh/id"):
            cert_path, key_path = cert_files.paths_for(hostile)
            for path in (cert_path, key_path):
                self.assertEqual(
                    os.path.dirname(os.path.realpath(path)),
                    os.path.realpath(cert_files.cert_root()),
                    f"{hostile!r} escaped to {path}",
                )

    def test_discard_removes_the_pair(self):
        cert_path, key_path = cert_files.materialize("zbook-east", CERT_PEM, KEY_PEM)
        cert_files.discard(cert_path, key_path)
        self.assertEqual(self._cert_files_on_disk(), [])
        cert_files.discard(cert_path, key_path)  # second call must be a quiet no-op


class ExitedWorkersDoNotStrandDirectories(CertFileIsolation):
    """A pid-scoped directory is only bounded if the directories of exited pids are reclaimed.

    The backend runs anyio process-pool workers, and anyio kills one after 300 seconds idle and
    spawns a replacement with a new pid, so new pid directories keep appearing.
    """

    def setUp(self):
        super().setUp()
        cert_files._last_sweep = float("-inf")
        self.addCleanup(setattr, cert_files, "_last_sweep", float("-inf"))

    def _sibling(self, pid: str) -> str:
        path = os.path.join(os.path.dirname(cert_files.cert_root()), pid)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "bliss01-deadbeef.crt"), "w") as handle:
            handle.write(CERT_PEM)
        return path

    def test_a_directory_from_an_exited_process_is_reclaimed(self):
        stale = self._sibling("999999")  # far above the container's pid range
        self.assertTrue(os.path.isdir(stale))

        cert_files.materialize("nuc2", CERT_PEM, KEY_PEM)

        self.assertFalse(
            os.path.isdir(stale),
            "a recycled worker's certificate directory was left behind, which is the same "
            "unbounded growth in a slower form",
        )

    def test_a_live_process_directory_is_left_alone(self):
        """Deleting a running sibling's certificates would break its open connections."""
        live = self._sibling("1")  # pid 1 is uvicorn itself, always alive in the container
        cert_files._last_sweep = float("-inf")

        cert_files.materialize("panda", CERT_PEM, KEY_PEM)

        self.assertTrue(os.path.isdir(live), "a live process had its certificates removed")

    def test_the_sweep_is_skipped_without_proc(self):
        """Absent /proc a missing entry proves nothing, so nothing may be deleted."""
        stale = self._sibling("999998")
        real_isdir = os.path.isdir

        def no_proc(path):
            return False if path == "/proc/self" else real_isdir(path)

        os.path.isdir = no_proc
        try:
            cert_files.materialize("center1-alpha", CERT_PEM, KEY_PEM)
        finally:
            os.path.isdir = real_isdir

        self.assertTrue(real_isdir(stale), "swept without being able to verify the pid was dead")


class StorageFailureIsNotAClusterVerdict(CertFileIsolation):
    def _fail_with(self, error):
        def raising(*args, **kwargs):
            raise error
        return raising

    def test_enospc_surfaces_as_a_storage_error_not_a_connection_error(self):
        original = tempfile.mkstemp
        tempfile.mkstemp = self._fail_with(OSError(errno.ENOSPC, "No space left on device"))
        try:
            with self.assertRaises(LXDLocalStorageError) as caught:
                cert_files.materialize("bliss001", CERT_PEM, KEY_PEM)
        finally:
            tempfile.mkstemp = original

        self.assertNotIsInstance(
            caught.exception, LXDConnectionError,
            "a full backend disk must not be catchable as a cluster connection failure - that is "
            "what made every healthy hypervisor read as offline",
        )
        self.assertIn("temporary storage is full", str(caught.exception).lower())

    def test_a_storage_failure_does_not_trip_the_circuit_breaker(self):
        """The breaker decides whether a cluster is skipped. A local disk fault must not vote."""
        manager = LXDConnectionManager()
        health.clear("bliss01")

        original = tempfile.mkstemp
        tempfile.mkstemp = self._fail_with(OSError(errno.ENOSPC, "No space left on device"))
        try:
            with self.assertRaises(LXDLocalStorageError):
                manager.connect_with_vault_cert(
                    cluster_id="bliss01",
                    endpoint="https://198.51.100.7:8443",
                    cert_content=CERT_PEM,
                    key_content=KEY_PEM,
                    force=True,
                )
        finally:
            tempfile.mkstemp = original
            health.clear("bliss01")

        self.assertFalse(
            health.is_tripped("bliss01"),
            "ENOSPC recorded the cluster as offline for the breaker TTL, which both mislabelled a "
            "healthy host and suppressed the retries that would have corrected it",
        )


class AProbeMustNotInventAClusterStatus(CertFileIsolation):
    """A probe that never ran has no verdict to return.

    derive_status reads reachable=None as OFFLINE, a default documented in cluster_status.py, so
    a probe returned after a local storage fault would make the check-auth endpoint report every
    healthy cluster as down. probe_connection raises instead.
    """

    def _fail_with(self, error):
        def raising(*args, **kwargs):
            raise error
        return raising

    def _manager(self):
        from app.services.lxd_cert_manager import LXDCertificateManager

        manager = LXDCertificateManager.__new__(LXDCertificateManager)
        manager.clusters_path = "secret/data/clusters"
        manager.vault = None
        manager.vault_read = lambda path: {"endpoint": "https://198.51.100.7:8443"}
        manager.get_cluster_certificate = lambda cluster_id: {
            "client.crt": CERT_PEM, "client.key": KEY_PEM
        }

        class _Vault:
            def vault_read(self, path):
                return {"endpoint": "https://198.51.100.7:8443"}

        manager.vault = _Vault()
        return manager

    def test_derive_status_is_why_a_returned_probe_would_lie(self):
        from app.models.cluster_status import ClusterStatus, derive_status

        status, _reason, _message = derive_status(connected=False, reachable=None)
        self.assertEqual(
            status, ClusterStatus.OFFLINE,
            "if this ever stops defaulting to OFFLINE, revisit whether probe_connection still "
            "needs to raise instead of returning",
        )

    def test_a_storage_fault_raises_instead_of_returning_a_probe(self):
        manager = self._manager()
        health.clear("nuc2")

        original = tempfile.mkstemp
        tempfile.mkstemp = self._fail_with(OSError(errno.ENOSPC, "No space left on device"))
        try:
            with self.assertRaises(LXDLocalStorageError):
                manager.probe_connection("nuc2", use_breaker=False)
        finally:
            tempfile.mkstemp = original
            health.clear("nuc2")

        self.assertFalse(health.is_tripped("nuc2"))

    def test_the_bool_wrapper_still_absorbs_it_for_its_loop_callers(self):
        """verify_connection is called per cluster while building warning lists.

        Letting the fault escape there would drop the whole list rather than one entry, so the
        bool wrapper absorbs it.
        """
        manager = self._manager()

        original = tempfile.mkstemp
        tempfile.mkstemp = self._fail_with(OSError(errno.ENOSPC, "No space left on device"))
        try:
            trusted, message = manager.verify_connection("nuc2", use_breaker=False)
        finally:
            tempfile.mkstemp = original
            health.clear("nuc2")

        self.assertFalse(trusted)
        self.assertIn("could not verify", message.lower())


class TheCertificateBaseIsPinned(unittest.TestCase):
    """A full tmpfs must fail instead of relocating the private keys.

    tempfile.gettempdir() probes for a writable directory and falls through to /var/tmp when
    /tmp is full. /tmp is the memory-backed tmpfs the compose file mounts for this material;
    /var/tmp is the container's disk layer, so gettempdir() would silently start writing TLS
    private keys to disk once the tmpfs filled.
    """

    def test_the_default_base_is_the_tmpfs_not_a_probe(self):
        self.assertEqual(cert_files.DEFAULT_CERT_BASE, "/tmp")

    def test_an_unusable_base_fails_rather_than_relocating_the_keys(self):
        """A base that cannot hold the files must raise instead of resolving somewhere else.

        Rooted at a regular file so the failure holds for root too, which is how both the suite
        and the container run.
        """
        with tempfile.NamedTemporaryFile(prefix="orcastra-not-a-dir-") as blocker:
            unusable = os.path.join(blocker.name, "certs")
            previous = os.environ.get(cert_files.CERT_BASE_ENV)
            os.environ[cert_files.CERT_BASE_ENV] = unusable
            try:
                self.assertTrue(cert_files.cert_root().startswith(unusable))
                with self.assertRaises(LXDLocalStorageError):
                    cert_files.materialize("nuc2", CERT_PEM, KEY_PEM)
            finally:
                if previous is None:
                    os.environ.pop(cert_files.CERT_BASE_ENV, None)
                else:
                    os.environ[cert_files.CERT_BASE_ENV] = previous


class UsageIsReportable(CertFileIsolation):
    def test_describe_usage_reports_the_numbers_health_needs(self):
        usage = cert_files.describe_usage()
        self.assertTrue(usage["available"])
        for key in ("used_mib", "total_mib", "percent_used", "degraded"):
            self.assertIn(key, usage)
        self.assertIsInstance(usage["degraded"], bool)


if __name__ == "__main__":
    unittest.main()
