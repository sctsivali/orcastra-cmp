"""Per-target rate limits for desktop actions.

Keyed per `(key, cluster, project, instance)` so one runaway agent on one guest cannot starve
another guest the same key drives, and raising a key's general limit does not widen this one.

Two buckets, because the costs differ. An action is one short command inside the guest. A capture
encodes and ships megabytes and costs the model about 1334 visual tokens to read, so twenty a
minute is generous for a see-act loop.
"""

import logging
from typing import Optional, Tuple

from app.middleware.security import _shared_allow

from .runtime import DESKTOP_ACTIONS_PER_MINUTE, DESKTOP_SCREENSHOTS_PER_MINUTE

logger = logging.getLogger(__name__)

__all__ = ["consume", "_bucket_key"]

_WINDOW_SECONDS = 60


def _bucket_key(kind: str, key_id: str, cluster_id: str, project: str, instance: str) -> str:
    return f"desktop:{kind}:{key_id}:{cluster_id}:{project}:{instance}"


def consume(
    *,
    key_id: str,
    cluster_id: str,
    project: str,
    instance: str,
    capture: bool,
    fail_closed: bool,
) -> Tuple[bool, Optional[int]]:
    """Spend one action against this target's budget. Returns (allowed, retry_after_seconds).

    A capture spends both buckets, so a screenshot loop cannot dodge the action ceiling.

    important: `fail_closed` is the one exception to the fail-open rule every other limiter keeps.
    `_shared_allow` explains that rule: a limiter that rejects when its store is unreachable turns a
    Redis outage into a product outage. Unbounded typing into a customer's desktop during an outage
    costs more than refusing it, so input fails closed.
    """
    checks = [("actions", DESKTOP_ACTIONS_PER_MINUTE)]
    if capture:
        checks.append(("captures", DESKTOP_SCREENSHOTS_PER_MINUTE))

    for kind, limit in checks:
        allowed = _shared_allow(
            _bucket_key(kind, key_id, cluster_id, project, instance), limit, _WINDOW_SECONDS
        )
        if allowed is None:
            if fail_closed:
                logger.warning(
                    "Refusing desktop input for key %s on %s/%s: the shared budget store cannot "
                    "answer, and input fails closed",
                    key_id, cluster_id, instance,
                )
                return False, _WINDOW_SECONDS
            continue
        if not allowed:
            return False, _WINDOW_SECONDS
    return True, None
