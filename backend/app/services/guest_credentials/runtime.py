"""Pool, budgets and caps for the credentials panel, kept beside the code that spends them.

important: its own thread pool, never `exec_executor` (`services/lxd/proxy.py`) or
`desktop_executor`. Each guest call holds a worker for its whole run, and a guest that stalls
until its timeout would otherwise take integration execs or desktop control down with it. On its
own pool the blast radius of a stalled guest is other credential requests, which shed with 429.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from app.core.blocking import run_blocking

T = TypeVar("T")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


POOL_WORKERS = _env_int("ORCASTRA_GUEST_CREDENTIALS_POOL_WORKERS", 4)
MAX_IN_FLIGHT = _env_int("ORCASTRA_GUEST_CREDENTIALS_MAX_IN_FLIGHT", 8)
MAX_IN_FLIGHT_PER_CLUSTER = _env_int("ORCASTRA_GUEST_CREDENTIALS_MAX_PER_CLUSTER", 3)

# One guest command. Reading /etc/passwd or writing one line is milliseconds; a guest that needs
# longer than this is wedged, and waiting longer only holds the worker.
EXEC_TIMEOUT = _env_float("ORCASTRA_GUEST_CREDENTIALS_EXEC_TIMEOUT", 30.0)
# Bound for what a guest may print to one read. An LDAP-sized /etc/passwd fits; a hostile guest
# printing forever is cut here, as frames arrive.
STDOUT_LIMIT = _env_int("ORCASTRA_GUEST_CREDENTIALS_STDOUT_LIMIT", 2 * 1024 * 1024)
# The two stdin calls (chpasswd, the key file write) print one status line.
STDIN_CALL_OUTPUT_LIMIT = 16 * 1024
# authorized_keys larger than this is refused rather than parsed: 256 KiB is over a thousand keys.
KEY_FILE_LIMIT = 256 * 1024
# Accounts the overview reports with a sudo verdict. More than this on a guest is a directory
# service, which this panel does not manage.
MAX_ACCOUNTS = 50

executor = ThreadPoolExecutor(max_workers=POOL_WORKERS, thread_name_prefix="guest_credentials")


async def blocking(fn: Callable[..., T], *args: Any, label: str, host: str, timeout: float = EXEC_TIMEOUT + 15) -> T:
    """One blocking step on this pool, with a per-cluster bound so one dead cluster sheds alone."""
    return await run_blocking(
        fn,
        *args,
        timeout=timeout,
        label=label,
        executor=executor,
        max_in_flight=MAX_IN_FLIGHT,
        concurrency_key=host,
        max_in_flight_per_key=MAX_IN_FLIGHT_PER_CLUSTER,
    )
