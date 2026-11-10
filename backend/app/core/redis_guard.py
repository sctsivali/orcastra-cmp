"""Skip a Redis that does not answer for SKIP_SECONDS at a time instead of waiting on every command.

The shared client is synchronous and most callers run on a worker's event loop, so a Redis that
accepts connections but does not answer (a paused container, a full disk, a partition) holds the
loop for the whole socket timeout on every command. A connection error or timeout opens the guard:
for SKIP_SECONDS every command raises `RedisSkipped` without touching the network and callers take
their Redis-down fallback. Then one command probes: an answer closes it, a failure reopens it.

important: a skipped command still raises; it never pretends to succeed. RedisSkipped is a redis
ConnectionError, so every `except redis.ConnectionError` and `except Exception` handler treats it
as an outage. The response cache's writes and invalidations go through `GuardedRedis.unskipped` and
are never skipped: they are how other workers learn an entry is stale.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

logger = logging.getLogger(__name__)

# How long a Redis that did not answer is left alone. Long enough that an outage costs each worker
# about one timeout per window, short enough that a recovered Redis is used again within seconds.
SKIP_SECONDS = 15.0
# How long the one probing command has before the others may try again, should it never return.
_PROBE_GRACE_SECONDS = 5.0
# Commands that hand back an object doing its own network I/O later.
_DEFERRED = frozenset({"pipeline", "register_script"})
_ITERATORS = frozenset({"scan_iter", "sscan_iter", "hscan_iter", "zscan_iter"})
# Local bookkeeping that sends nothing: never refused, so shutdown can close the pool mid-outage.
_LOCAL = frozenset(
    {"close", "get_encoder", "get_connection_kwargs", "get_retry", "set_retry", "pubsub", "lock"}
)
_FAILURES = (RedisConnectionError, RedisTimeoutError)


class RedisSkipped(RedisConnectionError):
    """Raised instead of sending a command while the guard leaves Redis alone."""


class GuardedRedis:
    """A redis.Redis proxy that skips Redis for SKIP_SECONDS after it fails to answer."""

    def __init__(
        self,
        client: Any,
        *,
        skip_seconds: float = SKIP_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._skip_seconds = skip_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # 0.0 while closed; otherwise the monotonic time the guard next lets one command through.
        self._open_until = 0.0

    # --- state ----------------------------------------------------------------------------------

    def available(self) -> bool:
        """False while the guard is skipping Redis. Never sends anything."""
        open_until = self._open_until
        return open_until == 0.0 or self._clock() >= open_until

    def _admit(self) -> None:
        open_until = self._open_until
        if open_until == 0.0:
            return
        with self._lock:
            now = self._clock()
            if now < self._open_until:
                raise RedisSkipped("Redis did not answer recently; skipped")
            # This caller probes. Everyone else keeps skipping until it answers or fails.
            self._open_until = now + _PROBE_GRACE_SECONDS

    def _failed(self, exc: BaseException) -> None:
        with self._lock:
            already_open = self._open_until != 0.0
            self._open_until = self._clock() + self._skip_seconds
        if not already_open:
            logger.warning(
                "Redis did not answer (%s); skipping it for %.0f s, local fallbacks serve meanwhile",
                type(exc).__name__, self._skip_seconds,
            )

    def _answered(self) -> None:
        if self._open_until == 0.0:
            return
        with self._lock:
            reopened = self._open_until != 0.0
            self._open_until = 0.0
        if reopened:
            logger.info("Redis answers again; using it")

    def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self._admit()
        return self._send(fn, *args, **kwargs)

    def _send(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            result = fn(*args, **kwargs)
        except RedisSkipped:
            raise
        except _FAILURES as exc:
            self._failed(exc)
            raise
        self._answered()
        return result

    # --- proxying -------------------------------------------------------------------------------

    @property
    def raw(self) -> Any:
        """The wrapped client, for tests and for code that must bypass the guard."""
        return self._client

    @property
    def unskipped(self) -> "_Unskipped":
        """The same client for writes that must never be skipped: each is sent, and still counted.

        important: an invalidation is the only way another worker learns that an entry is stale.
        Skipping one while this worker's guard is open would leave, ex; a revoked API key's
        metadata in Redis for its whole TTL while every other worker reads it as valid. A write is
        rare next to a read and is bounded by the 1 s timeout, so it is always attempted.
        """
        return _Unskipped(self)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if not callable(attr) or name in _LOCAL:
            return attr
        if name in _DEFERRED:
            return lambda *args, **kwargs: _Deferred(self, attr(*args, **kwargs))
        if name in _ITERATORS:
            return lambda *args, **kwargs: self._iterate(attr, *args, **kwargs)
        return lambda *args, **kwargs: self._call(attr, *args, **kwargs)

    def _iterate(self, fn: Callable[..., Any], *args: Any, admit: bool = True, **kwargs: Any):
        if admit:
            self._admit()
        try:
            yield from fn(*args, **kwargs)
        except RedisSkipped:
            raise
        except _FAILURES as exc:
            self._failed(exc)
            raise
        self._answered()


class _Unskipped:
    """GuardedRedis.unskipped: every command is sent, and its outcome still opens or closes the guard."""

    def __init__(self, guard: GuardedRedis) -> None:
        self._guard = guard

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._guard.raw, name)
        if not callable(attr) or name in _LOCAL:
            return attr
        if name in _ITERATORS:
            return lambda *args, **kwargs: self._guard._iterate(attr, *args, admit=False, **kwargs)
        return lambda *args, **kwargs: self._guard._send(attr, *args, **kwargs)


class _Deferred:
    """A pipeline or a registered script: its network call (execute, or the call itself) is guarded.

    Queuing a pipeline command sends nothing and returns the pipeline, so those calls pass through
    and hand back this wrapper to keep chaining guarded.
    """

    def __init__(self, guard: GuardedRedis, inner: Any) -> None:
        self._guard = guard
        self._inner = inner

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # important: a script run inside a pipeline must be handed the real Pipeline. redis-py
        # registers the script with the pipeline only on an isinstance check, and without that the
        # pipeline's EVALSHA fails with NOSCRIPT once Redis restarts and forgets its scripts.
        target = kwargs.get("client")
        if isinstance(target, _Deferred):
            kwargs["client"] = target._inner
            # Queued into a pipeline: nothing is sent until execute(), so this neither waits on
            # the guard nor tells it Redis answered.
            return self._inner(*args, **kwargs)
        return self._guard._call(self._inner, *args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._guard._call(self._inner.execute, *args, **kwargs)

    def __len__(self) -> int:
        return len(self._inner)

    def __bool__(self) -> bool:
        # A pipeline is always truthy in redis-py, whatever its length; a script has no length.
        return True

    def __enter__(self) -> "_Deferred":
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Optional[bool]:
        return self._inner.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            return self if result is self._inner else result

        return call


def answering(client: Any) -> bool:
    """True when there is a client and its guard, if it has one, is not skipping Redis."""
    if client is None:
        return False
    available = getattr(client, "available", None)
    return True if not callable(available) else bool(available())


def connect(url: str) -> GuardedRedis:
    """The shared synchronous client: short timeouts, no retries, behind the guard.

    No retries: redis-py retries a timed-out command once when retry_on_timeout is set, which
    doubles the wait. A local Redis answers in well under a millisecond, so a second without an
    answer is an outage, and the guard handles outages.
    """
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    return GuardedRedis(
        redis.from_url(
            url,
            decode_responses=True,
            max_connections=50,
            socket_connect_timeout=1,
            socket_timeout=1,
            socket_keepalive=True,
            retry=Retry(NoBackoff(), 0),
            health_check_interval=30,
        )
    )
