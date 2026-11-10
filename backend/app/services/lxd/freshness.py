"""Live reads of the LXD resources body, with the time they were taken.

pylxd memoises `/1.0/resources` on the client object: the `Client.resources` property fills
`self._resource_cache` on first access and never invalidates it. Clients are pooled for the
life of the process (connection.py: no TTL-based refresh), so every reader of
`client.resources` gets the body captured when that worker first connected, and each worker
holds its own frozen snapshot. Every read here is an explicit round trip, and `collected_at`
is taken as the read returns, so a cached body carries its collection time.

No timeout argument: `client.api.<verb>` routes through pylxd's _APINode, which injects the
client timeout, and connection.py mounts a TimeoutHTTPAdapter that backstops it.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

import pylxd
from requests import exceptions as requests_exc


@dataclass(frozen=True)
class LiveRead:
    """One `/1.0/resources` body and the moment it came off the cluster."""

    body: Dict[str, Any]
    collected_at: datetime


def read_resources(client: pylxd.Client, cluster_id: str) -> LiveRead:
    """Read a cluster's resources body now, bypassing pylxd's permanent memo.

    Thread the returned body through every consumer of one request. Calling this
    once per consumer multiplies the round trips and renders different samples side by side,
    so memory and GPU counts disagree across endpoints.
    """
    response = client.api.resources.get()
    collected_at = datetime.now(timezone.utc)
    if response.status_code != 200:
        raise RuntimeError(
            f"Resources read for {cluster_id} returned HTTP {response.status_code}"
        )
    return LiveRead(body=response.json()["metadata"], collected_at=collected_at)


def resources_failure(error: BaseException) -> Tuple[bool, str]:
    """(reachable, message) for a resources read that failed after host_info answered.

    important: only a refusal is an authorization problem. derive_status turns an answering
    host without resources into NOT_AUTHORIZED with the trust-add hint, so a timeout reported
    as "Not authorized" would send an operator to re-trust a certificate that is fine. A
    transport failure means the host did not answer this read; any other error means it
    answered without its resources. Neither is cached as untrusted.
    """
    if isinstance(error, (requests_exc.Timeout, requests_exc.ConnectionError)):
        return False, "Did not answer the resources read in time. It is asked again on the next check."
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status in (401, 403):
        return True, f"Not authorized: {error}"
    detail = f"HTTP {status}" if status else type(error).__name__
    return False, f"Answered without its resources ({detail}). It is asked again on the next check."
