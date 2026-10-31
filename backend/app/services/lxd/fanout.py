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

import logging
from dataclasses import dataclass, field
from typing import Dict, Generic, Iterable, List, Mapping, Optional, Sequence, Tuple, TypeVar

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

    @property
    def cacheable(self) -> bool:
        """Only a fan-out where every queried cluster answered in full may be remembered."""
        return bool(self.answered) and not self.partial_nodes and not self.unreachable_nodes

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


def _absent_reason(cluster: str) -> str:
    """Why a cluster the listing never read is missing, from the breaker's verdict.

    important: the failure kind only. last_failure() also holds str(exc), which can carry the
    endpoint and TLS detail this module's reasons promise never to.
    """
    failure = health.last_failure(cluster)
    if failure is None:
        return f"{KIND_UNREACHABLE}: not connected from this server yet"
    if failure[1] == health.KIND_UNAUTHORIZED:
        return f"{KIND_FORBIDDEN}: certificate not trusted by the cluster"
    return f"{KIND_UNREACHABLE}: no connection to the cluster"


# Fixed text for a cluster shared with another organization. Partial detail and LXD's own error
# text can name that organization's pools and volumes; a transport reason is already fixed text.
_BOUND_TEXT = {
    KIND_PARTIAL: f"{KIND_PARTIAL}: some rows could not be read",
    KIND_FORBIDDEN: f"{KIND_FORBIDDEN}: the cluster refused this read",
    KIND_FAILED: f"{KIND_FAILED}: the cluster could not answer this read",
}


def complete_reasons(
    unreachable: Iterable[str],
    known: Mapping[str, str],
    *,
    partial: Iterable[str] = (),
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
