"""A per-account budget for the routes that probe a cluster endpoint and issue a certificate.

Registration sends an unauthenticated GET to an endpoint the caller types and asks Vault PKI for
a client certificate. Clusters live on private networks, so the endpoint is not filtered; this
budget is what stops one account from mapping the networks the backend reaches or draining PKI.
It is keyed on the signed-in account because a client address is only as good as the proxy trust
in front of it.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

from fastapi import HTTPException, status

BUDGET = 20
WINDOW_SECONDS = 600

_lock = threading.Lock()
_local: Dict[str, List[float]] = {}


def _spend_locally(bucket: str) -> tuple[int, int]:
    now = time.monotonic()
    with _lock:
        recent = [t for t in _local.get(bucket, []) if now - t < WINDOW_SECONDS]
        recent.append(now)
        _local[bucket] = recent
        return len(recent), max(1, int(WINDOW_SECONDS - (now - recent[0])))


def spend_registration_budget(kind: str, user: Any) -> None:
    """Spend one attempt of `kind` for this account, or answer 429 once the window is spent."""
    from app.middleware.security import _shared_consume

    who = str(getattr(user, "email", None) or getattr(user, "user_id", None) or "anonymous").strip().lower()
    bucket = f"cluster-{kind}:{who}"
    spent = _shared_consume(bucket, BUDGET, WINDOW_SECONDS)
    count, reset = spent if spent is not None else _spend_locally(bucket)
    if count > BUDGET:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(reset)},
            detail={
                "error": "registration_rate_limited",
                "message": (
                    f"This account has started {BUDGET} cluster registrations or certificate "
                    f"renewals in {WINDOW_SECONDS // 60} minutes. Try again in "
                    f"{max(1, round(reset / 60))}m."
                ),
            },
        )


def forget_registration_budget() -> None:
    """Tests only: drop the in-process fallback counts."""
    with _lock:
        _local.clear()
