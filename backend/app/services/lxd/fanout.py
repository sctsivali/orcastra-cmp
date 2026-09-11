"""
One classification for what a per-cluster listing came back with.

Every list router fans out one helper per cluster and pairs the results with the cluster
names. Before this module each helper caught `Exception` and returned an empty list, so a
cluster whose listing failed (LXD answered 400 or 403, the read timed out after the
connection probe succeeded, a row did not validate) was counted as "answered" and its empty
answer was cached for the full TTL. On a production MicroCloud that read as "no storage
pools", "no volumes", "no identities" while the cluster held dozens of each.

The rule now: a cluster is either fully read, partially read (some rows lost, named), or not
read at all (named in `unreachable_nodes` with a reason). Nothing degraded is cached.

important: reasons are shown in the console banner. They never carry a host name, an
endpoint or the text of a transport error (both can name the host); LXD's own error text is
kept, truncated, because it is what an operator needs ("More than one cluster member has a
volume named ..."). Tenants get no reason at all: the text can name pools and volumes from
projects they cannot see, and transport diagnostics are operator information.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Generic, List, Optional, Sequence, Tuple, TypeVar

import requests
import urllib3
from pylxd.exceptions import LXDAPIException

from app.services.lxd.exceptions import (
    LXDAuthorizationError,
    LXDConnectionError,
    LXDLocalStorageError,
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
        if isinstance(reason, urllib3.exceptions.ConnectTimeoutError):
            return "connect timed out"
        if isinstance(reason, urllib3.exceptions.SSLError):
            return "TLS handshake failed"
        if isinstance(reason, urllib3.exceptions.NewConnectionError):
            return "connection refused or host unreachable"
        return "connection error"
    if isinstance(exc, requests.exceptions.Timeout):
        return "read timed out"
    if isinstance(exc, urllib3.exceptions.ReadTimeoutError):
        return "read timed out"
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

    @property
    def cacheable(self) -> bool:
        """Only a fan-out where every queried cluster answered in full may be remembered."""
        return bool(self.answered) and not self.partial_nodes and not self.unreachable_nodes


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
            fan.node_errors[node_name] = f"{kind}: {reason}"
            logger.warning(
                "Could not list %s on %s (%s: %s)", noun, node_name, kind, reason,
                exc_info=result if kind == KIND_FAILED else None,
            )
        else:
            fan.unreachable_nodes.append(node_name)
            fan.node_errors[node_name] = f"{KIND_FAILED}: unexpected {type(result).__name__}"
            logger.error("Helper for %s on %s returned %r", noun, node_name, type(result))
    if not reasons_visible:
        fan.node_errors = {}
    return fan
