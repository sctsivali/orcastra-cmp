"""
One classification for what a per-cluster listing came back with.

Every list router fans out one helper per cluster and pairs the results with the cluster
names. Before this module each helper caught `Exception` and returned an empty list, so a
cluster whose listing failed (LXD answered 400 or 403, the read timed out after the
connection probe succeeded, a row did not validate) was counted as "answered" and its empty
answer was cached for the full TTL. On a production MicroCloud that read as "no storage
pools", "no volumes", "no identities" while the cluster held dozens of each.

The rule now: a cluster is either fully read, partially read (some rows lost, named), or not
read at all (named in `unreachable_nodes` with a reason). A partial read or an LXD error answer
is never cached. A fan-out where some clusters could not be reached at all, and every other
cluster answered in full, may be cached briefly by the handlers that opt in
(`cacheable_despite_outages`): the body names those clusters, so it is a true answer.

important: reasons are shown in the console banner. They never carry a host name, an
endpoint or the text of a transport error (both can name the host); LXD's own error text is
kept, truncated, because it is what an operator needs ("More than one cluster member has a
volume named ..."). Tenants get no reason at all: the text can name pools and volumes from
projects they cannot see, and transport diagnostics are operator information.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generic, Iterable, List, Mapping, Optional, Sequence, Tuple, TypeVar

import requests
import urllib3
from pylxd.exceptions import LXDAPIException

from app.core.org_scope import visible_projects_for
from app.core.tenant_visibility import tenant_acl_active
from app.services.lxd import health
from app.services.lxd.exceptions import (
    LXDAuthorizationError,
    LXDConnectionError,
    LXDLocalStorageError,
    LXDOperationError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

KIND_UNREACHABLE = "unreachable"
KIND_FORBIDDEN = "forbidden"
KIND_FAILED = "error"
KIND_PARTIAL = "partial"
KIND_LATE = "late"

# How long a fleet answer missing a cluster that did not answer within its wall is remembered:
# briefly, so the next read asks that cluster again soon, but not "never", which would make every
# poll pay the whole wall again.
LATE_FRESH_SECONDS = 10

# LXD's error field, truncated. Long enough for "More than one cluster member has a volume
# named backups (specify a target member)", short enough that a stack trace cannot ride in.
_LXD_TEXT_LIMIT = 200


@dataclass
class NodeRead(Generic[T]):
    """What one helper read from one cluster: the rows, and what it could not read."""

    rows: List[T] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    def lost(self, what: str) -> None:
        self.problems.append(what)

    @property
    def complete(self) -> bool:
        return not self.problems


def transport_reason(exc: BaseException) -> Optional[str]:
    """A fixed phrase for a requests/urllib3 transport failure, or None if it is not one.

    note: the pooled adapter mounts Retry(read=0), so urllib3 exhausts on the first read
    timeout and requests surfaces it as ConnectionError wrapping MaxRetryError whose reason
    is ReadTimeoutError. Unwrapped here so a read timeout is named as one.
    """
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "read timed out"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "connect timed out"
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS handshake failed"
    if isinstance(exc, requests.exceptions.ConnectionError):
        reason = _urllib3_reason(exc)
        if isinstance(reason, urllib3.exceptions.ReadTimeoutError):
            return "read timed out"
        # note: NewConnectionError (refused, no route, DNS) subclasses ConnectTimeoutError in
        # urllib3, so it has to be told apart first or it reads as a timeout.
        if isinstance(reason, urllib3.exceptions.NewConnectionError):
            return "connection refused or host unreachable"
        if isinstance(reason, urllib3.exceptions.ConnectTimeoutError):
            return "connect timed out"
        if isinstance(reason, urllib3.exceptions.SSLError):
            return "TLS handshake failed"
        return "connection error"
    if isinstance(exc, requests.exceptions.Timeout):
        return "read timed out"
    if isinstance(exc, urllib3.exceptions.ReadTimeoutError):
        return "read timed out"
    if isinstance(exc, urllib3.exceptions.NewConnectionError):
        return "connection refused or host unreachable"
    if isinstance(exc, urllib3.exceptions.ConnectTimeoutError):
        return "connect timed out"
    if isinstance(exc, (urllib3.exceptions.HTTPError, requests.exceptions.RequestException)):
        return "connection error"
    return None


def _urllib3_reason(exc: requests.exceptions.ConnectionError):
    inner = exc.args[0] if exc.args else None
    if isinstance(inner, urllib3.exceptions.MaxRetryError):
        return inner.reason
    return inner


def is_read_timeout(exc: BaseException) -> bool:
    """True for a read timeout in any of the shapes requests hands it back in."""
    return transport_reason(exc) == "read timed out"


def _lxd_status(exc: LXDAPIException) -> Optional[int]:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def _lxd_text(exc: LXDAPIException) -> str:
    try:
        text = str(exc)
    except Exception:  # a response without a body
        text = ""
    text = " ".join(text.split())
    return text[:_LXD_TEXT_LIMIT]


def classify(exc: BaseException) -> Tuple[str, str]:
    """Map an exception from a listing helper to (kind, reason).

    The kind decides where the cluster is reported; the reason is what the banner shows.
    """
    if isinstance(exc, LXDAuthorizationError):
        return KIND_FORBIDDEN, "certificate not trusted by the cluster"
    if isinstance(exc, LXDConnectionError):
        return KIND_UNREACHABLE, "no connection to the cluster"
    if isinstance(exc, LXDLocalStorageError):
        return KIND_FAILED, "backend could not prepare TLS credentials (local storage)"
    if isinstance(exc, LXDAPIException):
        status = _lxd_status(exc)
        text = _lxd_text(exc)
        if status == 403:
            return KIND_FORBIDDEN, f"LXD answered 403: {text}" if text else "LXD answered 403"
        if status is None:
            return KIND_FAILED, f"LXD error: {text}" if text else "LXD error"
        return KIND_FAILED, f"LXD answered {status}: {text}" if text else f"LXD answered {status}"
    transport = transport_reason(exc)
    if transport is not None:
        return KIND_UNREACHABLE, transport
    return KIND_FAILED, type(exc).__name__


@dataclass
class Fanout(Generic[T]):
    """The merged result of one fan-out."""

    rows: List[T] = field(default_factory=list)
    answered: List[str] = field(default_factory=list)
    partial_nodes: List[str] = field(default_factory=list)
    unreachable_nodes: List[str] = field(default_factory=list)
    node_errors: Dict[str, str] = field(default_factory=dict)
    # Failure kind per cluster in unreachable_nodes, kept even when node_errors is withheld.
    failure_kinds: Dict[str, str] = field(default_factory=dict)
    # Clusters still reading past their wall (gather_clusters): not unreachable, not in this
    # answer yet, and filled in by the background read that finishes them.
    late_nodes: List[str] = field(default_factory=list)

    @property
    def cacheable(self) -> bool:
        """Only a fan-out where every queried cluster answered in full may be remembered."""
        return (
            bool(self.answered)
            and not self.partial_nodes
            and not self.unreachable_nodes
            and not self.late_nodes
        )

    @property
    def cacheable_despite_outages(self) -> bool:
        """Complete, or complete apart from clusters that could not be reached at all.

        important: a transport failure (connect or read timeout, refused, TLS) leaves no rows to
        misread, and the body names the cluster, so remembering it for a short degraded TTL is
        remembering a true answer. Without this a single timing-out cluster made every read of
        the storage volume list cost the full 25 s read budget. A 4xx/5xx from LXD or a partial
        read is still never cached: those look like an answer while missing rows.
        """
        if not self.answered or self.partial_nodes:
            return False
        return all(
            self.failure_kinds.get(node) == KIND_UNREACHABLE for node in self.unreachable_nodes
        )


# What a fleet read says about a cluster it skipped because this worker knows it is down. Fixed
# text by kind: the recorded reason is raw exception text and can name the endpoint.
_DOWN_TEXT = {
    health.KIND_UNAUTHORIZED: "certificate not trusted by the cluster",
    health.KIND_OFFLINE: "no connection to the cluster",
}


def down_text(kind: Optional[str]) -> str:
    return _DOWN_TEXT.get(kind or health.KIND_OFFLINE, _DOWN_TEXT[health.KIND_OFFLINE])


def known_down(cluster_id: str, connection_manager: object = None) -> Optional[str]:
    """The failure kind when this worker knows the cluster is down and holds no client for it.

    important: a fleet read must not dial such a cluster. Once its 60 s trip lapsed, the next read
    that reached it paid a whole connect (about 8 s against a silent host on dev) and its response
    waited for it; 29 of the 32 such dials in one day came from a single integration poll. The
    background healer re-probes the cluster and clears the verdict on its first success, and the
    forced paths (Check Authentication, Verify, renew) still dial. A cluster this worker never saw
    fail answers None and is still dialed once.

    note: the memory read comes first, so a manager is only consulted for a cluster that failed. A
    manager without get_cached_client (a stub) counts as holding no client.
    """
    verdict = health.last_failure(cluster_id)
    if verdict is None:
        return None
    cached = getattr(connection_manager, "get_cached_client", None)
    if cached is not None and cached(cluster_id) is not None:
        return None
    return verdict[1]


def _absent_reason(cluster: str) -> str:
    """Why a cluster the listing never read is missing, from the breaker's verdict.

    important: the failure kind only. last_failure() also holds str(exc), which can carry the
    endpoint and TLS detail this module's reasons promise never to.
    """
    failure = health.last_failure(cluster)
    if failure is None:
        return f"{KIND_UNREACHABLE}: not connected from this server yet"
    if failure[1] == health.KIND_UNAUTHORIZED:
        return f"{KIND_FORBIDDEN}: {down_text(failure[1])}"
    return f"{KIND_UNREACHABLE}: {down_text(failure[1])}"


# Fixed text for a cluster shared with another organization. Partial detail and LXD's own error
# text can name that organization's pools and volumes; a transport reason is already fixed text.
_BOUND_TEXT = {
    KIND_LATE: f"{KIND_LATE}: still reading, shown when it finishes",
    KIND_PARTIAL: f"{KIND_PARTIAL}: some rows could not be read",
    KIND_FORBIDDEN: f"{KIND_FORBIDDEN}: the cluster refused this read",
    KIND_FAILED: f"{KIND_FAILED}: the cluster could not answer this read",
}


def complete_reasons(
    unreachable: Iterable[str],
    known: Mapping[str, str],
    *,
    partial: Iterable[str] = (),
    late: Iterable[str] = (),
) -> Dict[str, str]:
    """`node_errors` for a list answer: one reason per missing or partial cluster, for this caller.

    Clusters reach unreachable_nodes without passing through collect() (no client in this worker,
    a breaker skip, a registered cluster nobody asked), and they used to carry nothing, so the
    banner printed "no reason reported" beside the one cluster that had a reason.

    A Tenant gets nothing. On a cluster this caller sees only part of, the text is reduced to its
    kind; visible_projects_for answers None for an Admin and on a single-organization cluster, so
    the full diagnosis stays with whoever may read it.
    """
    if tenant_acl_active():
        return {}
    reasons: Dict[str, str] = {}
    for cluster in unreachable:
        reasons[cluster] = known.get(cluster) or _absent_reason(cluster)
    for cluster in partial:
        reasons[cluster] = known.get(cluster) or _BOUND_TEXT[KIND_PARTIAL]
    for cluster in late:
        reasons[cluster] = known.get(cluster) or _BOUND_TEXT[KIND_LATE]
    return {
        cluster: _BOUND_TEXT.get(text.partition(":")[0], text)
        if visible_projects_for(cluster) is not None
        else text
        for cluster, text in reasons.items()
    }


def failure_text(cluster: str, failure: BaseException) -> Optional[str]:
    """`error` for a list that reports one row per cluster, in complete_reasons' terms.

    important: never str(failure). LXDOperationError is a sentence repeating what a service caught,
    endpoint included, so the reason comes from what it wraps. None for a Tenant, like the lists.
    """
    while isinstance(failure, LXDOperationError) and (failure.__cause__ or failure.__context__):
        failure = failure.__cause__ or failure.__context__
    kind, reason = classify(failure)
    return complete_reasons([cluster], {cluster: f"{kind}: {reason}"}).get(cluster)


def unattempted(seeded: Sequence[str], connection_manager: object) -> List[str]:
    """The seeded clusters this worker has never tried to connect to.

    A list handler seeds `unreachable_nodes` with every registered cluster it holds no client
    for. Two very different things put a cluster there: it is down (this worker tried, and
    the healer keeps trying), or this worker has not caught up with a registration yet (the
    next healer tick connects it, within a minute). A body naming a known-down cluster is a
    correct answer and may be cached, as it always was; a body naming a cluster the worker
    has simply not tried must not be, because the cache is shared by every worker and the
    one that did connect it would then serve "unreachable" for the whole TTL.

    A manager without `has_attempted` (a stub) is treated as having tried nothing, which is
    the conservative reading: no cache rather than a wrong one.
    """
    tried = getattr(connection_manager, "has_attempted", None)
    if tried is None:
        return list(seeded)
    return [node for node in seeded if not tried(node)]


def collect(
    nodes: Sequence[str],
    results: Sequence[object],
    *,
    noun: str,
    reasons_visible: bool = True,
) -> Fanout:
    """Pair asyncio.gather(return_exceptions=True) output with the clusters it was asked for.

    A NodeRead with problems is a partial read; an exception is classified; anything else is
    a programming error and is reported as failed rather than silently accepted.
    `reasons_visible=False` (Tenant callers) keeps the lists and drops the texts.
    """
    fan: Fanout = Fanout()
    for node_name, result in zip(nodes, results):
        if isinstance(result, LateRead):
            fan.late_nodes.append(node_name)
            fan.node_errors[node_name] = (
                f"{KIND_LATE}: still reading after {round(result.wall)} s, shown when it finishes"
            )
            logger.info("%s listing on %s is still reading after %.0f s", noun, node_name, result.wall)
            continue
        if isinstance(result, NodeRead):
            fan.rows.extend(result.rows)
            fan.answered.append(node_name)
            if not result.complete:
                fan.partial_nodes.append(node_name)
                detail = "; ".join(result.problems)
                fan.node_errors[node_name] = f"{KIND_PARTIAL}: {detail}"
                logger.warning("Partial %s listing on %s: %s", noun, node_name, detail)
        elif isinstance(result, BaseException):
            kind, reason = classify(result)
            fan.unreachable_nodes.append(node_name)
            fan.failure_kinds[node_name] = kind
            fan.node_errors[node_name] = f"{kind}: {reason}"
            logger.warning(
                "Could not list %s on %s (%s: %s)", noun, node_name, kind, reason,
                exc_info=result if kind == KIND_FAILED else None,
            )
        else:
            fan.unreachable_nodes.append(node_name)
            fan.failure_kinds[node_name] = KIND_FAILED
            fan.node_errors[node_name] = f"{KIND_FAILED}: unexpected {type(result).__name__}"
            logger.error("Helper for %s on %s returned %r", noun, node_name, type(result))
    if not reasons_visible:
        fan.node_errors = {}
    return fan


# --- A wall per cluster, and a read that outlives it finishes in the background ------------------

# Owner decision 2026-11-04: a fleet list waits at most this long for any one cluster.
CLUSTER_WALL_SECONDS = 15.0
# How long a late cluster's finished read stands in for it in later answers.
LATE_KEEP_SECONDS = 180.0
# A stand-in older than this is read again in the background while it keeps answering.
LATE_REREAD_AFTER_SECONDS = 90.0
# The background re-read's own budget: about the slowest listing measured on dev (central-1's
# volumes, 24 s) with room to spare.
LATE_REREAD_TIMEOUT_SECONDS = 60.0
_LATE_MAX_ENTRIES = 256


class LateRead:
    """A gather_clusters result slot for a cluster still reading past its wall."""

    def __init__(self, node: str, wall: float) -> None:
        self.node = node
        self.wall = wall


@dataclass
class _Late:
    generation: Any
    pending: Optional["asyncio.Future[Any]"] = None
    value: Any = None
    finished_at: Optional[float] = None


_late_lock = threading.Lock()
_late: "OrderedDict[str, _Late]" = OrderedDict()


def _late_entry(key: str) -> Optional[_Late]:
    with _late_lock:
        return _late.get(key)


def _remember_late(key: str, entry: _Late) -> None:
    with _late_lock:
        _late[key] = entry
        _late.move_to_end(key)
        while len(_late) > _LATE_MAX_ENTRIES:
            _late.popitem(last=False)


def _forget_late(key: str, entry: _Late) -> None:
    with _late_lock:
        if _late.get(key) is entry:
            _late.pop(key, None)


def _late_finished(key: str, entry: _Late, future: "asyncio.Future[Any]", on_done: Optional[Callable[[], None]]) -> None:
    entry.pending = None
    if future.cancelled() or future.exception() is not None:
        # A late read that failed is no stand-in; the next answer reads the cluster again.
        _forget_late(key, entry)
        return
    entry.value = future.result()
    entry.finished_at = time.monotonic()
    # note: only an entry still held schedules the rebuild. One evicted meanwhile (the memo is
    # bounded) would otherwise buy a rebuild that cannot use it and reads the cluster again.
    if on_done is not None and _late_entry(key) is entry:
        on_done()


async def gather_clusters(
    nodes: Sequence[str],
    fn: Callable[..., Any],
    *args: Any,
    executor: Any,
    label: str,
    memo_key: str,
    generation: Optional[Callable[[], Any]] = None,
    wall: Optional[float] = None,
    max_in_flight: Optional[int] = None,
) -> List[object]:
    """asyncio.gather(..., return_exceptions=True) over clusters, with a wall per cluster.

    A cluster still reading at the wall answers LateRead (collect() lists it in late_nodes),
    and its read runs on. When it finishes, its result stands in for that cluster in later answers
    of the same list and scope for LATE_KEEP_SECONDS, and one background rebuild of the entry
    being built is scheduled (swr.capture_refresh), so the rows appear without a poll missing
    first. A stand-in older than LATE_REREAD_AFTER_SECONDS is read again in the background while
    it keeps answering, so a cluster that is always slow (central-1's volumes take 17 to 24 s on
    dev) is never missing after its first read. A second request meeting a read still running
    waits on it instead of starting another.

    important: keyed by the caller's scope (get_scope_cache_key) and fenced by `generation`, like
    the response cache: a stand-in never crosses a scope, and a mutation discards it. Only a late
    cluster ever gets one; every other cluster is read every time. `fn(node, *args)` runs through
    run_blocking, so it gets the caller's context and needs its own socket timeouts.
    """
    from app.core import swr
    from app.core.blocking import BlockingTimeout, run_blocking
    from app.core.org_scope import get_scope_cache_key

    budget = CLUSTER_WALL_SECONDS if wall is None else wall
    # A forced refresh (fresh=true) reads every cluster itself: a stand-in is a cached answer.
    forced = swr.building_forced()
    token = generation() if generation is not None else None
    scope = get_scope_cache_key(f"late:{memo_key}")
    on_done = swr.capture_refresh()

    def reread_in_background(node: str, key: str, entry: _Late) -> None:
        async def reread() -> None:
            try:
                value = await run_blocking(
                    fn, node, *args, timeout=LATE_REREAD_TIMEOUT_SECONDS,
                    label=f"{label}[{node}] reread", executor=executor, max_in_flight=max_in_flight,
                )
            except Exception as exc:  # noqa: BLE001 - the stand-in keeps answering until it lapses
                logger.info("Background re-read of %s on %s failed: %s", label, node, type(exc).__name__)
                return
            if _late_entry(key) is entry and (generation is None or generation() == token):
                entry.value = value
                entry.finished_at = time.monotonic()
            entry.pending = None

        entry.pending = asyncio.ensure_future(reread())

    async def one(node: str) -> object:
        key = f"{scope}:{node}"
        entry = None if forced else _late_entry(key)
        if entry is not None and entry.generation != token:
            _forget_late(key, entry)
            entry = None
        if entry is not None and entry.finished_at is not None:
            age = time.monotonic() - entry.finished_at
            if age <= LATE_KEEP_SECONDS:
                if age > LATE_REREAD_AFTER_SECONDS and entry.pending is None:
                    reread_in_background(node, key, entry)
                return entry.value
            _forget_late(key, entry)
            entry = None
        if entry is not None and entry.pending is not None and not entry.pending.done():
            try:
                return await asyncio.wait_for(asyncio.shield(entry.pending), timeout=budget)
            except asyncio.TimeoutError:
                return LateRead(node, budget)
            except Exception as exc:  # noqa: BLE001 - classified by collect()
                return exc
        try:
            return await run_blocking(
                fn, node, *args, timeout=budget, label=f"{label}[{node}]",
                executor=executor, max_in_flight=max_in_flight,
            )
        except BlockingTimeout as exc:
            if exc.pending is None:
                return exc
            late = _Late(generation=token, pending=exc.pending)
            _remember_late(key, late)
            exc.pending.add_done_callback(lambda future: _late_finished(key, late, future, on_done))
            return LateRead(node, budget)
        except Exception as exc:  # noqa: BLE001 - classified by collect()
            return exc

    return list(await asyncio.gather(*(one(node) for node in nodes)))


# --- Waiting for a slot rather than being refused one -------------------------------------------

_slots: Dict[Tuple[int, str], asyncio.Semaphore] = {}


def _slot(name: str, size: int) -> asyncio.Semaphore:
    """One semaphore per pool and event loop (each test's asyncio.run has its own loop)."""
    key = (id(asyncio.get_running_loop()), name)
    slot = _slots.get(key)
    if slot is None:
        slot = _slots[key] = asyncio.Semaphore(size)
    return slot


async def run_in_slot(name: str, size: int, fn: Callable[..., Any], *args: Any, timeout: float, label: str, executor: Any) -> Any:
    """run_blocking, waiting for one of `size` slots first instead of failing when none is free.

    important: run_blocking's max_in_flight refuses (BlockingPoolFull) at once, so a gather over
    more clusters than the cap dropped every cluster past it: a key granting five clusters always
    lost one from its totals. This queues instead, and the wall starts once the call has a slot.
    A slot is given back when the thread really finishes, not when its wall passes, so a slow
    cluster cannot let more calls in than the pool has threads.
    """
    from app.core.blocking import BlockingTimeout, run_blocking

    slot = _slot(name, size)
    await slot.acquire()
    handed_off = False
    try:
        return await run_blocking(fn, *args, timeout=timeout, label=label, executor=executor)
    except BlockingTimeout as exc:
        if exc.pending is not None:
            exc.pending.add_done_callback(lambda _future: slot.release())
            handed_off = True
        raise
    finally:
        if not handed_off:
            slot.release()
