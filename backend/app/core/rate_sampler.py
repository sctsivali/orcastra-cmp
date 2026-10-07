"""Per-second rates from cumulative counters, shared by every worker through Redis.

Counters are the source of truth. A rate is derived once, over a window this module controls,
and stored beside its baseline so every worker, viewer and surface reads the same figure.

important: the baseline must not live in one process. The backend runs several uvicorn workers
and a poll lands on whichever worker takes it, so a per-process baseline answered "calculating"
once per worker and a cold worker's first figure averaged over however long it had been since
that worker last saw the instance, hours included.

Rules, in the order `advance` applies them:
  1. no baseline, another epoch (restart, migration) or a baseline older than `max_age`:
     seed a new one and answer "calculating";
  2. a sample older than the baseline, or one inside `min_window` of it: answer with the stored
     window and leave the baseline where it is (two tabs, a manual refresh, a fleet sweep right
     after a dialog poll), so a millisecond window can never turn into a 0% or 800% spike;
  3. otherwise a rate for every counter present in both samples that did not go down. A counter
     that went down was reset, so it gets no rate for this window.

Writes are compare-and-set on the sample time, so a slow request can never replace a newer
baseline with an older one. A request that loses that race still answers with its own rates,
which are valid for its own window.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

KEY_PREFIX = "orcastra:rate:v1"
LOCAL_MAX_ENTRIES = 5000
REDIS_COOLDOWN_SECONDS = 30.0

# KEYS[1] = baseline hash; ARGV = sample time, serialized window, ttl seconds.
_CAS_SCRIPT = """
local t = redis.call('HGET', KEYS[1], 't')
if t and tonumber(t) >= tonumber(ARGV[1]) then return 0 end
redis.call('HSET', KEYS[1], 't', ARGV[1], 'v', ARGV[2])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return 1
"""


@dataclass(frozen=True)
class Sample:
    """One read of cumulative counters. `taken_at` is wall-clock seconds at the read."""

    taken_at: float
    epoch: str
    counters: Mapping[str, float]


@dataclass(frozen=True)
class SamplerPolicy:
    min_window: float = 1.5
    max_age: float = 120.0
    ttl_seconds: int = 180


@dataclass(frozen=True)
class RateReading:
    status: Literal["ready", "calculating"]
    rates: Dict[str, float] = field(default_factory=dict)
    window_seconds: Optional[float] = None


@dataclass(frozen=True)
class StoredWindow:
    """The baseline plus the rates last computed against it."""

    taken_at: float
    epoch: str
    counters: Dict[str, float]
    rates: Dict[str, float]
    window_seconds: Optional[float]

    def view(self) -> RateReading:
        if self.window_seconds is None:
            return RateReading("calculating")
        return RateReading("ready", dict(self.rates), self.window_seconds)

    def dumps(self) -> str:
        return json.dumps(
            {
                "t": self.taken_at,
                "e": self.epoch,
                "c": self.counters,
                "r": self.rates,
                "w": self.window_seconds,
            },
            separators=(",", ":"),
        )

    @classmethod
    def loads(cls, raw: Optional[str]) -> Optional["StoredWindow"]:
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return cls(
                taken_at=float(data["t"]),
                epoch=str(data["e"]),
                counters={str(k): float(v) for k, v in dict(data["c"]).items()},
                rates={str(k): float(v) for k, v in dict(data.get("r") or {}).items()},
                window_seconds=None if data.get("w") is None else float(data["w"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            logger.debug("Discarding an unreadable rate baseline: %s", exc)
            return None


def advance(
    stored: Optional[StoredWindow],
    sample: Sample,
    *,
    min_window: float,
    max_age: float,
) -> Tuple[RateReading, Optional[StoredWindow]]:
    """Pure step: what this sample reads as, and the window to store (None = store nothing)."""
    counters = {key: float(value) for key, value in sample.counters.items()}
    seeded = StoredWindow(sample.taken_at, sample.epoch, counters, {}, None)
    if stored is None or stored.epoch != sample.epoch:
        return RateReading("calculating"), seeded
    gap = sample.taken_at - stored.taken_at
    if gap > max_age:
        return RateReading("calculating"), seeded
    if gap < min_window:
        # covers gap < 0 too: an older read arriving late never moves the baseline back
        return stored.view(), None
    rates: Dict[str, float] = {}
    for key, current in counters.items():
        previous = stored.counters.get(key)
        if previous is None or current < previous:
            continue
        rates[key] = (current - previous) / gap
    window = round(gap, 3)
    return RateReading("ready", rates, window), StoredWindow(
        sample.taken_at, sample.epoch, counters, rates, window
    )


class LocalSampleStore:
    """In-process fallback with the same compare-and-set rule and a bounded, expiring keyspace."""

    def __init__(self, max_entries: int = LOCAL_MAX_ENTRIES) -> None:
        self._entries: "OrderedDict[str, Tuple[float, StoredWindow]]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def get_many(self, keys: list[str]) -> list[Optional[StoredWindow]]:
        now = time.monotonic()
        found: list[Optional[StoredWindow]] = []
        with self._lock:
            for key in keys:
                entry = self._entries.get(key)
                if entry is None or entry[0] <= now:
                    self._entries.pop(key, None)
                    found.append(None)
                    continue
                found.append(entry[1])
        return found

    def put_many(self, writes: list[Tuple[str, StoredWindow]], ttl_seconds: int) -> None:
        now = time.monotonic()
        with self._lock:
            for key, window in writes:
                current = self._entries.get(key)
                if current is not None and current[0] > now and current[1].taken_at >= window.taken_at:
                    continue
                self._entries.pop(key, None)
                self._entries[key] = (now + ttl_seconds, window)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class RedisSampleStore:
    """Baselines as Redis hashes {t, v}; writes go through one compare-and-set script."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._cas = client.register_script(_CAS_SCRIPT)

    def get_many(self, keys: list[str]) -> list[Optional[StoredWindow]]:
        pipe = self._client.pipeline(transaction=False)
        for key in keys:
            pipe.hget(key, "v")
        return [StoredWindow.loads(raw) for raw in pipe.execute()]

    def put_many(self, writes: list[Tuple[str, StoredWindow]], ttl_seconds: int) -> None:
        if not writes:
            return
        pipe = self._client.pipeline(transaction=False)
        for key, window in writes:
            self._cas(keys=[key], args=[repr(window.taken_at), window.dumps(), ttl_seconds], client=pipe)
        pipe.execute()


def _redis_client() -> Any:
    """The shared Redis client, or None. Patched to None by the test suite."""
    from app.core.cache import get_cache_manager

    return getattr(get_cache_manager(), "redis_client", None)


_SAMPLERS: "list[RateSampler]" = []


def reset_all_samplers() -> None:
    """Forget every sampler's in-process state. The test suite calls this around each test."""
    for sampler in list(_SAMPLERS):
        sampler.reset_local()


class RateSampler:
    """Rates for many identities in one namespace. Blocking: call from executor threads only."""

    def __init__(self, namespace: str, policy: SamplerPolicy) -> None:
        self.namespace = namespace
        self.policy = policy
        self._local = LocalSampleStore()
        self._redis_store: Optional[RedisSampleStore] = None
        self._redis_owner: Any = None
        self._redis_retry_at = 0.0
        self._lock = threading.Lock()
        _SAMPLERS.append(self)

    def key(self, identity: str) -> str:
        return f"{KEY_PREFIX}:{self.namespace}:{identity}"

    def observe(self, identity: str, sample: Sample, *, min_window: Optional[float] = None) -> RateReading:
        return self.observe_many({identity: sample}, min_window=min_window)[identity]

    def observe_many(
        self, samples: Mapping[str, Sample], *, min_window: Optional[float] = None
    ) -> Dict[str, RateReading]:
        if not samples:
            return {}
        identities = list(samples)
        keys = [self.key(identity) for identity in identities]
        store = self._shared_store()
        if store is not None:
            try:
                return self._observe(store, identities, keys, samples, min_window)
            except Exception as exc:  # noqa: BLE001 - any Redis failure degrades to local
                self._redis_failed(exc)
        return self._observe(self._local, identities, keys, samples, min_window)

    def reset_local(self) -> None:
        """Forget in-process baselines and any Redis cooldown. For tests and incident tooling."""
        self._local.clear()
        with self._lock:
            self._redis_store = None
            self._redis_owner = None
            self._redis_retry_at = 0.0

    def _observe(
        self,
        store: Any,
        identities: list[str],
        keys: list[str],
        samples: Mapping[str, Sample],
        min_window: Optional[float],
    ) -> Dict[str, RateReading]:
        window_floor = self.policy.min_window if min_window is None else min_window
        stored = store.get_many(keys)
        readings: Dict[str, RateReading] = {}
        writes: list[Tuple[str, StoredWindow]] = []
        for identity, key, previous in zip(identities, keys, stored):
            reading, window = advance(
                previous, samples[identity], min_window=window_floor, max_age=self.policy.max_age
            )
            readings[identity] = reading
            if window is not None:
                writes.append((key, window))
        store.put_many(writes, self.policy.ttl_seconds)
        return readings

    def _shared_store(self) -> Optional[RedisSampleStore]:
        with self._lock:
            if time.monotonic() < self._redis_retry_at:
                return None
        try:
            client = _redis_client()
        except Exception as exc:  # noqa: BLE001
            self._redis_failed(exc)
            return None
        if client is None:
            return None
        with self._lock:
            if self._redis_store is None or self._redis_owner is not client:
                self._redis_store = RedisSampleStore(client)
                self._redis_owner = client
            return self._redis_store

    def _redis_failed(self, exc: Exception) -> None:
        with self._lock:
            already_cooling = time.monotonic() < self._redis_retry_at
            self._redis_retry_at = time.monotonic() + REDIS_COOLDOWN_SECONDS
            self._redis_store = None
            self._redis_owner = None
        if not already_cooling:
            logger.warning(
                "Rate sampler %s fell back to in-process baselines for %.0fs: %s",
                self.namespace,
                REDIS_COOLDOWN_SECONDS,
                exc,
            )
