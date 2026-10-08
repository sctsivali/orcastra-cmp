"""Monitoring history on a Redis sorted set: one member per point, scored by its timestamp.

important: each scope's history used to be one JSON string, read and rewritten whole on every
append (3 to 7 MB for the admin scope) and parsed on the event loop by every chart read. A sorted
set takes one member per append and trims by score, and a window read fetches only the points
inside the window. Decoding, validation and encoding run on this module's executors, and an
encoded body is reused until the selection's version changes.

note: every write goes through one script that adds a point only when its timestamp is not stored
yet, so the previous release's string key (imported once per scope) and the Postgres copy of the
admin scope merge at any time without duplicating a point. Reads and writes use separate
executors, so a burst of chart reads can never refuse the collector's write. The keys live under
orcastra:monitoring_history:, which the cache-clear endpoint already deletes.
"""

import json
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from pydantic import TypeAdapter, ValidationError

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.models.schemas import MetricsHistoryPoint
from app.services.monitoring_history import (
    DEFAULT_WINDOW_SECONDS,
    HISTORY_MAX_POINTS,
    HISTORY_NAMESPACE,
    HISTORY_RETENTION,
    HISTORY_TTL_SECONDS,
    parse_timestamp,
)

logger = logging.getLogger(__name__)

POINTS_PREFIX = "orcastra:monitoring_history:points"
IMPORT_LOCK_PREFIX = "orcastra:monitoring_history:points_import_lock"
IMPORT_DONE_PREFIX = "orcastra:monitoring_history:points_imported"
HYDRATE_LOCK_PREFIX = "orcastra:monitoring_history:points_hydrate_lock"
LOCK_SECONDS = 300
# The admin scope refills from Postgres when its oldest point is this much younger than the
# retention, which is what a Redis restart, an eviction or a fresh install leaves behind.
HYDRATE_GAP = timedelta(minutes=10)
LOCAL_MAX_POINTS = 4320
LOCAL_MAX_SCOPES = 8
MEMO_MAX_BYTES = 24 * 1024 * 1024
RETENTION_MS = int(HISTORY_RETENTION.total_seconds() * 1000)
_WRITE_CHUNK = 500
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_POINT_LIST = TypeAdapter(List[MetricsHistoryPoint])
_read_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="monitoring_history_read")
_write_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="monitoring_history_write")

# KEYS[1] the points key. ARGV: oldest score kept, most points kept, ttl, then score/member pairs.
_WRITE_SCRIPT = """
local added = 0
for i = 4, #ARGV, 2 do
  if redis.call('ZCOUNT', KEYS[1], ARGV[i], ARGV[i]) == 0 then
    redis.call('ZADD', KEYS[1], ARGV[i], ARGV[i + 1])
    added = added + 1
  end
end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', '(' .. ARGV[1])
redis.call('ZREMRANGEBYRANK', KEYS[1], 0, -(tonumber(ARGV[2]) + 1))
redis.call('EXPIRE', KEYS[1], ARGV[3])
return added
"""

Row = Tuple[int, str]


def points_key(history_key: str) -> str:
    return f"{POINTS_PREFIX}:{history_key}"


def score_of(point: Any) -> Optional[int]:
    """Epoch milliseconds of a point's timestamp, exact for microsecond timestamps."""
    stamp = parse_timestamp(point.get("timestamp")) if isinstance(point, dict) else None
    if stamp is None:
        return None
    return (stamp - _EPOCH) // timedelta(milliseconds=1)


def _rows(points: Iterable[Any], now_ms: int) -> List[Row]:
    """(score, member) for each point still inside the retention, oldest first."""
    rows = []
    for point in points:
        score = score_of(point)
        if score is not None and score >= now_ms - RETENTION_MS:
            rows.append((score, json.dumps(point, separators=(",", ":"), ensure_ascii=False, default=str)))
    return sorted(rows, key=lambda row: row[0])


class Stats(NamedTuple):
    card: int
    count: int
    oldest_ms: Optional[int]
    newest_ms: Optional[int]


@dataclass(frozen=True)
class HistoryRead:
    body: bytes
    total: int
    oldest_ms: Optional[int]
    # False when Redis failed and this worker's buffer answered: refills would only time out too.
    store_ok: bool = True


class LocalPoints:
    """Per-process points for while Redis is unavailable: bounded per scope and in scopes."""

    def __init__(self) -> None:
        self._scopes: "OrderedDict[str, List[Row]]" = OrderedDict()
        self._lock = threading.Lock()

    def write(self, key: str, rows: Sequence[Row], *, now_ms: int) -> int:
        with self._lock:
            current = dict(self._scopes.pop(key, []))
            fresh = {score: member for score, member in rows if score not in current}
            current.update(fresh)
            kept = sorted(row for row in current.items() if row[0] >= now_ms - RETENTION_MS)
            self._scopes[key] = kept[-LOCAL_MAX_POINTS:]
            while len(self._scopes) > LOCAL_MAX_SCOPES:
                self._scopes.popitem(last=False)
            return len(fresh)

    def _selected(self, key: str, since_ms: Optional[int], last: Optional[int]) -> Tuple[List[Row], List[Row]]:
        with self._lock:
            rows = list(self._scopes.get(key, []))
        if last is not None:
            return rows, rows[-last:]
        return rows, [row for row in rows if row[0] >= (since_ms or 0)]

    def stats(self, key: str, *, since_ms: Optional[int], last: Optional[int]) -> Stats:
        rows, selected = self._selected(key, since_ms, last)
        return Stats(len(rows), len(selected), rows[0][0] if rows else None, rows[-1][0] if rows else None)

    def select(self, key: str, *, since_ms: Optional[int], last: Optional[int]) -> List[str]:
        return [member for _, member in self._selected(key, since_ms, last)[1]]

    def clear(self) -> None:
        with self._lock:
            self._scopes.clear()


class _Memo:
    """Encoded bodies by selection, released oldest first past MEMO_MAX_BYTES."""

    def __init__(self) -> None:
        self._entries: "OrderedDict[Tuple[str, str, int], Tuple[str, bytes]]" = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: Tuple[str, str, int], version: str) -> Optional[bytes]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry[0] != version:
                return None
            self._entries.move_to_end(key)
            return entry[1]

    def put(self, key: Tuple[str, str, int], version: str, body: bytes) -> None:
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= len(previous[1])
            self._entries[key] = (version, body)
            self._bytes += len(body)
            while self._bytes > MEMO_MAX_BYTES and len(self._entries) > 1:
                _, (_, dropped) = self._entries.popitem(last=False)
                self._bytes -= len(dropped)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0


_local = LocalPoints()
_memo = _Memo()
_imported: Set[str] = set()
_local_locks: Dict[str, float] = {}


def _redis(cache: Any) -> Any:
    return getattr(cache, "redis_client", None)


async def _run(fn: Callable[..., Any], *args: Any, label: str, timeout: float, reads: bool = False) -> Any:
    return await run_blocking(
        fn,
        *args,
        timeout=timeout,
        label=label,
        executor=_read_executor if reads else _write_executor,
        max_in_flight=64 if reads else 16,
    )


def _write_blocking(cache: Any, history_key: str, rows: Sequence[Row], now_ms: int) -> int:
    """Add the rows whose timestamps are not stored yet; the number added."""
    if not rows:
        return 0
    client = _redis(cache)
    if client is not None:
        try:
            script = client.register_script(_WRITE_SCRIPT)
            head = [now_ms - RETENTION_MS, HISTORY_MAX_POINTS, HISTORY_TTL_SECONDS]
            added = 0
            for start in range(0, len(rows), _WRITE_CHUNK):
                pairs = [part for row in rows[start : start + _WRITE_CHUNK] for part in row]
                added += int(script(keys=[points_key(history_key)], args=head + pairs))
            return added
        except Exception as exc:  # noqa: BLE001 - a lost Redis write costs this worker's buffer
            logger.warning("History write kept in this worker only, Redis failed: %s", exc)
    return _local.write(history_key, rows, now_ms=now_ms)


async def record_point(cache: Any, history_key: str, point: Dict[str, Any]) -> bool:
    """Store one point for its scope; False when it was not stored."""
    now_ms = int(time.time() * 1000)
    try:
        added = await _run(
            _write_blocking, cache, history_key, _rows([point], now_ms), now_ms,
            label="monitoring.history.write", timeout=12.0,
        )
    except (BlockingTimeout, BlockingPoolFull) as exc:
        logger.warning("History point for %s not stored: %s", history_key, exc)
        return False
    return added > 0


def _selection(
    window_seconds: Optional[int], points: Optional[int], now_ms: int
) -> Tuple[Optional[int], Optional[int]]:
    """(since_ms, last): the legacy `points` count wins only when no window was asked for."""
    if window_seconds is None and points is not None:
        return None, points
    return now_ms - (window_seconds or DEFAULT_WINDOW_SECONDS) * 1000, None


def _first_score(rows: Sequence[Tuple[Any, float]]) -> Optional[int]:
    return int(rows[0][1]) if rows else None


def _redis_stats(client: Any, key: str, since_ms: Optional[int], last: Optional[int]) -> Stats:
    pipe = client.pipeline(transaction=False)
    pipe.zcard(key)
    pipe.zrange(key, 0, 0, withscores=True)
    pipe.zrange(key, -1, -1, withscores=True)
    if last is None:
        pipe.zcount(key, since_ms, "+inf")
    card, oldest, newest, *window = pipe.execute()
    count = int(window[0]) if window else min(int(card), last or 0)
    return Stats(int(card), count, _first_score(oldest), _first_score(newest))


def _redis_members(client: Any, key: str, since_ms: Optional[int], last: Optional[int]) -> List[str]:
    if last is not None:
        return list(client.zrange(key, -last, -1))
    return list(client.zrangebyscore(key, since_ms, "+inf"))


def _encode(members: Sequence[Any]) -> bytes:
    models: List[MetricsHistoryPoint] = []
    for raw in members:
        try:
            models.append(MetricsHistoryPoint.model_validate_json(raw))
        except (ValidationError, ValueError):
            logger.debug("Skipping an unreadable history point")
    return _POINT_LIST.dump_json(models)


def _answer(
    memo_key: Tuple[str, str, int], stats: Stats, fetch: Callable[[], List[str]], *, store_ok: bool
) -> HistoryRead:
    version = f"{'store' if store_ok else 'local'}:{'/'.join(map(str, stats))}"
    body = _memo.get(memo_key, version)
    if body is None:
        body = _encode(fetch())
        _memo.put(memo_key, version, body)
    return HistoryRead(body, stats.card, stats.oldest_ms, store_ok)


def _read_blocking(
    cache: Any, history_key: str, window_seconds: Optional[int], points: Optional[int], now_ms: int
) -> HistoryRead:
    since_ms, last = _selection(window_seconds, points, now_ms)
    kind = "last" if last is not None else "window"
    memo_key = (history_key, kind, last or window_seconds or DEFAULT_WINDOW_SECONDS)
    client = _redis(cache)
    if client is not None:
        try:
            key = points_key(history_key)
            stats = _redis_stats(client, key, since_ms, last)
            fetch = lambda: _redis_members(client, key, since_ms, last)  # noqa: E731
            return _answer(memo_key, stats, fetch, store_ok=True)
        except Exception as exc:  # noqa: BLE001 - read this worker's buffer instead
            logger.warning("History read from this worker only, Redis failed: %s", exc)
    stats = _local.stats(history_key, since_ms=since_ms, last=last)
    fetch = lambda: _local.select(history_key, since_ms=since_ms, last=last)  # noqa: E731
    return _answer(memo_key, stats, fetch, store_ok=client is None)


async def history_body(
    cache: Any, history_key: str, *, window_seconds: Optional[int], points: Optional[int]
) -> HistoryRead:
    """The encoded points of a window (or the last `points`), plus the scope's total and oldest."""
    return await _run(
        _read_blocking, cache, history_key, window_seconds, points, int(time.time() * 1000),
        label="monitoring.history.read", timeout=15.0, reads=True,
    )


def needs_hydrate(read: HistoryRead, *, now_ms: Optional[int] = None) -> bool:
    """Whether the stored history starts well after the retention window does."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    gap_ms = int(HYDRATE_GAP.total_seconds() * 1000)
    return read.oldest_ms is None or read.oldest_ms > now_ms - RETENTION_MS + gap_ms


def _claim(cache: Any, key: str, seconds: int) -> bool:
    """SET NX EX on Redis; without Redis the same rule held per process."""
    client = _redis(cache)
    if client is not None:
        return bool(client.set(key, "1", nx=True, ex=seconds))
    now = time.time()
    if _local_locks.get(key, 0.0) > now:
        return False
    _local_locks[key] = now + seconds
    return True


def _import_blocking(cache: Any, history_key: str, now_ms: int) -> Optional[int]:
    """Points imported from the previous string key, or None while another worker imports."""
    client = _redis(cache)
    if client is not None and client.exists(f"{IMPORT_DONE_PREFIX}:{history_key}"):
        return 0
    if not _claim(cache, f"{IMPORT_LOCK_PREFIX}:{history_key}", LOCK_SECONDS):
        return None
    legacy = cache.get(HISTORY_NAMESPACE, history_key)
    points = [point for point in legacy if isinstance(point, dict)] if isinstance(legacy, list) else []
    added = _write_blocking(cache, history_key, _rows(points, now_ms), now_ms)
    if client is not None:
        client.set(f"{IMPORT_DONE_PREFIX}:{history_key}", "1", ex=HISTORY_TTL_SECONDS)
    return added


async def import_previous_history(cache: Any, history_key: str) -> int:
    """Merge the scope's points from the previous release's string key, once per scope."""
    if history_key in _imported:
        return 0
    try:
        added = await _run(
            _import_blocking, cache, history_key, int(time.time() * 1000),
            label="monitoring.history.import", timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001 - the chart works without the old points
        logger.debug("Previous history for %s not imported yet: %s", history_key, exc)
        return 0
    if added is None:
        return 0
    _imported.add(history_key)
    if added:
        logger.info("Imported %d history points for %s from the previous store", added, history_key)
    return added


async def hydrate_from_postgres(cache: Any, history_key: str) -> int:
    """Merge the admin scope's last 24 hours from Postgres; one worker per lock period."""
    from app.services.metrics_history_service import load_metrics_history

    try:
        lock = f"{HYDRATE_LOCK_PREFIX}:{history_key}"
        if not await _run(_claim, cache, lock, LOCK_SECONDS, label="monitoring.history.lock", timeout=12.0):
            return 0
        since = datetime.now(timezone.utc) - HISTORY_RETENTION
        points = await load_metrics_history(points=HISTORY_MAX_POINTS, scope="all", since=since)
        now_ms = int(time.time() * 1000)
        added = await _run(
            _write_blocking, cache, history_key, _rows(points, now_ms), now_ms,
            label="monitoring.history.hydrate", timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001 - the chart works from what Redis holds
        logger.warning("History for %s not refilled from Postgres: %s", history_key, exc)
        return 0
    if added:
        logger.info("Refilled %d history points for %s from Postgres", added, history_key)
    return added


def reset_local_state() -> None:
    """Forget this process's fallback points, encoded bodies, import marks and locks. For tests."""
    _local.clear()
    _memo.clear()
    _imported.clear()
    _local_locks.clear()
