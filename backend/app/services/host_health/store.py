"""Where Host Health keeps what outlives one pass: the last report, each check's since-time, and the
Fluent Bit samples that delivery is judged against.

Redis when it answers, so four workers serve one report and agree on when a check changed. The
since-time is a compare-and-set in one Lua call: whichever worker observes a change first records
it, and only that worker reports the transition, so the audit trail gets one row, not four.

When Redis does not answer, each worker keeps its own copy (`mode == "process"`). Since-times may
then differ between workers, and the caller logs transitions without writing audit rows, because
every worker would write the same one.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

REPORT_KEY = "orcastra:host_health:report:v1"
SINCE_KEY = "orcastra:host_health:since:v1"
SAMPLES_KEY = "orcastra:host_health:fluentbit:v1"
# At least 15 s apart (checks/logging.py), so 130 samples cover 32 minutes or more: longer than the
# largest stall window the settings allow (HOST_HEALTH_LOG_STALL_SECONDS <= 1800).
MAX_SAMPLES = 130

# Returns {changed, since, previous}. `since` travels as a string: Lua numbers come back to Redis
# clients truncated to integers.
_MARK = """
local old = redis.call('HGET', KEYS[1], ARGV[1])
if old then
  local decoded = cjson.decode(old)
  if decoded['status'] == ARGV[2] then return {0, tostring(decoded['since']), decoded['status']} end
  redis.call('HSET', KEYS[1], ARGV[1], cjson.encode({status = ARGV[2], since = ARGV[3]}))
  return {1, ARGV[3], decoded['status']}
end
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode({status = ARGV[2], since = ARGV[3]}))
return {1, ARGV[3], ''}
"""


@dataclass(frozen=True)
class Transition:
    check_id: str
    previous: Optional[str]
    status: str
    since: float


class HostHealthStore:
    def __init__(self, redis: Callable[[], Any]) -> None:
        self._redis = redis
        self._lock = threading.Lock()
        self._report: Optional[Dict[str, Any]] = None
        self._since: Dict[str, Tuple[str, float]] = {}
        self._samples: List[Dict[str, Any]] = []
        self.mode = "shared"

    def _client(self) -> Any:
        client = self._redis()
        if client is None:
            raise ConnectionError("Redis is not configured")
        return client

    def _fallback(self, what: str, exc: Exception) -> None:
        if self.mode != "process":
            logger.warning("Host Health store fell back to this worker (%s): %s", what, type(exc).__name__)
        self.mode = "process"

    # -- report ------------------------------------------------------------------------------

    def read_report(self) -> Optional[Dict[str, Any]]:
        try:
            raw = self._client().get(REPORT_KEY)
            self.mode = "shared"
            return json.loads(raw) if raw else None
        except Exception as exc:  # noqa: BLE001 - any store failure falls back, never raises
            self._fallback("read report", exc)
            with self._lock:
                return dict(self._report) if self._report else None

    def write_report(self, report: Dict[str, Any], ttl_seconds: int) -> None:
        with self._lock:
            self._report = report
        try:
            self._client().set(REPORT_KEY, json.dumps(report), ex=ttl_seconds)
            self.mode = "shared"
        except Exception as exc:  # noqa: BLE001
            self._fallback("write report", exc)

    # -- since-times -------------------------------------------------------------------------

    def mark(self, check_id: str, status: str, now: float) -> Tuple[float, Optional[Transition]]:
        """The check's since-time, and a Transition when this call is the one that saw it change."""
        try:
            client = self._client()
            changed, since_raw, previous = client.register_script(_MARK)(
                keys=[SINCE_KEY], args=[check_id, status, repr(now)]
            )
            self.mode = "shared"
            since = float(since_raw)
            with self._lock:
                self._since[check_id] = (status, since)
            if int(changed):
                return since, Transition(check_id, previous or None, status, since)
            return since, None
        except Exception as exc:  # noqa: BLE001
            self._fallback("since", exc)
        with self._lock:
            old = self._since.get(check_id)
            if old is not None and old[0] == status:
                return old[1], None
            self._since[check_id] = (status, now)
            return now, Transition(check_id, old[0] if old else None, status, now)

    # -- Fluent Bit samples ------------------------------------------------------------------

    def push_sample(self, sample: Dict[str, Any]) -> None:
        with self._lock:
            self._samples = (self._samples + [sample])[-MAX_SAMPLES:]
        try:
            client = self._client()
            pipe = client.pipeline()
            pipe.rpush(SAMPLES_KEY, json.dumps(sample))
            pipe.ltrim(SAMPLES_KEY, -MAX_SAMPLES, -1)
            pipe.expire(SAMPLES_KEY, 3600)
            pipe.execute()
            self.mode = "shared"
        except Exception as exc:  # noqa: BLE001
            self._fallback("push sample", exc)

    def samples(self) -> List[Dict[str, Any]]:
        try:
            raw = self._client().lrange(SAMPLES_KEY, 0, -1)
            self.mode = "shared"
            out: List[Dict[str, Any]] = []
            for item in raw or []:
                try:
                    out.append(json.loads(item))
                except (TypeError, ValueError):
                    continue
            return out
        except Exception as exc:  # noqa: BLE001
            self._fallback("read samples", exc)
            with self._lock:
                return list(self._samples)


def _shared_redis() -> Any:
    from app.core.cache import get_cache_manager

    return getattr(get_cache_manager(), "redis_client", None)


_store: Optional[HostHealthStore] = None


def get_store() -> HostHealthStore:
    global _store
    if _store is None:
        _store = HostHealthStore(_shared_redis)
    return _store
