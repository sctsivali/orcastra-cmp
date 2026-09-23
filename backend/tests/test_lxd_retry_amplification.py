"""
Encodes the retry-amplification measurement that drives the timeout design.

`LXDConnectionManager._configure_client_session` mounts an HTTPAdapter carrying
`Retry(total=3, connect=0, backoff_factor=0.5)` onto every pylxd session. `read` is left
unset, so it inherits `total`, and a read timeout is therefore retried three more times.

The consequence that matters: a socket timeout of T does NOT bound wall clock. Measured on
the dev VM against a real accept-and-never-answer socket, T=5s produced 23.0s and T=8s
produced 35.0s, four TCP connections each. That is why the fix needs both a socket timeout
and a wall-clock cap, and why `read=0` is worth setting.

These tests assert the attempt COUNT rather than wall clock, so they are deterministic.
"""

import socket
import threading
import unittest

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Mirrors the idempotent-method retry policy in backend/app/services/lxd/connection.py.
# Mutating methods are deliberately absent: a timeout after submission is an unknown outcome.
PRODUCTION_RETRY = dict(
    total=3,
    connect=0,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "OPTIONS"],
    raise_on_status=False,
)


class _SilentServer:
    """Accepts connections and never writes a byte. This is the production failure mode.

    A peer that sends RST does not reproduce the bug: requests fails fast and the caller
    survives. Silence is what parks the thread.
    """

    def __init__(self) -> None:
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(64)
        self.port: int = self._srv.getsockname()[1]
        self.accepted = 0
        self._held: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept_forever, daemon=True)
        self._thread.start()

    def _accept_forever(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self._held.append(conn)
            self.accepted += 1

    def close(self) -> None:
        self._srv.close()
        for conn in self._held:
            conn.close()


def _session_with(retry_kwargs: dict) -> requests.Session:
    session = requests.Session()
    session.mount("http://", HTTPAdapter(max_retries=Retry(**retry_kwargs)))
    return session


class RetryAmplificationTests(unittest.TestCase):
    def test_production_retry_turns_one_call_into_four_attempts(self) -> None:
        server = _SilentServer()
        self.addCleanup(server.close)
        session = _session_with(PRODUCTION_RETRY)

        with self.assertRaises(requests.exceptions.RequestException):
            session.get(f"http://127.0.0.1:{server.port}/1.0", timeout=(3, 0.2))

        self.assertEqual(
            4,
            server.accepted,
            "the pooled Retry turns one logical call into four TCP attempts, so wall clock "
            "is roughly 4*read_timeout plus backoff",
        )

    def test_read_disabled_collapses_to_a_single_attempt(self) -> None:
        """The contract the fix relies on: read=0 re-raises instead of retrying."""
        server = _SilentServer()
        self.addCleanup(server.close)
        session = _session_with({**PRODUCTION_RETRY, "read": 0})

        with self.assertRaises(requests.exceptions.RequestException):
            session.get(f"http://127.0.0.1:{server.port}/1.0", timeout=(3, 0.2))

        self.assertEqual(
            1,
            server.accepted,
            "with read=0 a read timeout is re-raised immediately, so wall clock is ~T",
        )


if __name__ == "__main__":
    unittest.main()
