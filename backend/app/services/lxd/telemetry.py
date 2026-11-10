"""Where an LXD call came from and how long it took, for the operator log.

The access log's one latency per request cannot say which cluster or call spent it, or whether a
request or the background healer paid for a dial. So a slow LXD request (at or above
ORCASTRA_LXD_SLOW_MS, 1000 ms by default) logs one INFO line with the cluster, method, path,
listing parameters, status and time to the response headers, and a connect attempt adds
`lxd_dial` (cluster, origin, ms, outcome) to the line it already writes, so dials can be counted
by who paid for them.

important: both carry fixed vocabularies and the cluster id only, never an endpoint, a host or
an exception's text. The origin comes from the thread name: a bare run_in_executor does not
carry the request context into the thread.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlsplit

logger = logging.getLogger(__name__)

# Thread-name prefixes of the background work that dials clusters, and what to call it.
_ORIGINS = (
    ("metrics_bg", "healer"),
    ("lxd-breaker", "breaker_sync"),
    ("lxd-dead-probe", "dead_probe"),
    ("monitoring_", "monitoring"),
)
# Listing parameters worth keeping: they decide how heavy a read is.
_KEPT_PARAMS = ("recursion", "all-projects", "project", "target")


def slow_request_ms() -> int:
    try:
        return max(0, int(os.environ.get("ORCASTRA_LXD_SLOW_MS", "1000")))
    except ValueError:
        return 1000


def origin() -> str:
    """The background origin `_ORIGINS` names, or `request` for everything a caller waits on."""
    name = threading.current_thread().name
    for prefix, label in _ORIGINS:
        if name.startswith(prefix):
            return label
    return "request"


def dial_fields(cluster_id: str, started: float, outcome: str) -> Dict[str, Any]:
    """`extra` for a connect log line. `started` is a time.monotonic() reading."""
    return {
        "extra_fields": {
            "lxd_dial": {
                "cluster": cluster_id,
                "origin": origin(),
                "ms": round((time.monotonic() - started) * 1000),
                "outcome": outcome,
            }
        }
    }


def _request_target(url: str) -> str:
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k in _KEPT_PARAMS]
    query = "&".join(f"{k}={v}" for k, v in kept)
    return f"{parts.path}?{query}" if query else parts.path


def _slow_request_hook(cluster_id: str):
    def hook(response, *args, **kwargs):
        elapsed = getattr(response, "elapsed", None)
        if elapsed is None:
            return response
        ms = round(elapsed.total_seconds() * 1000)
        if ms < slow_request_ms():
            return response
        request = getattr(response, "request", None)
        method = getattr(request, "method", "") or ""
        target = _request_target(getattr(request, "url", "") or "")
        status: Optional[int] = getattr(response, "status_code", None)
        logger.info(
            "Slow LXD request on %s: %s %s %d ms -> %s",
            cluster_id, method, target, ms, status,
            extra={
                "extra_fields": {
                    "lxd_request": {
                        "cluster": cluster_id,
                        "method": method,
                        "target": target,
                        "status": status,
                        "ms": ms,
                        "origin": origin(),
                    }
                }
            },
        )
        return response

    hook.orcastra_slow_request_for = cluster_id  # type: ignore[attr-defined]
    return hook


def install_slow_request_log(session: Any, cluster_id: Optional[str]) -> None:
    """Add the slow-request hook to a pooled client's session, once per cluster."""
    if not cluster_id:
        return
    hooks = session.hooks.setdefault("response", [])
    if any(getattr(h, "orcastra_slow_request_for", None) == cluster_id for h in hooks):
        return
    hooks.append(_slow_request_hook(cluster_id))
