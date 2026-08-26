"""Live reads of the LXD resources body, with the time they were taken.

pylxd memoises `/1.0/resources` on the client object and never invalidates it:

    self._resource_cache = None          # Client.__init__

    @property
    def resources(self):
        if self._resource_cache is None:
            ...
            self._resource_cache = response.json()["metadata"]
        return self._resource_cache

This app pools clients for the life of the process on purpose (connection.py: "NO TTL-based
refresh (connections persist indefinitely while valid)"), so every reader of `client.resources`
got the body captured when that worker first connected. With WEB_CONCURRENCY=4 there were four
frozen snapshots per cluster, and whichever worker served a cache miss wrote its own into
Redis. Measured on the dev fleet, the shared regions body cycled through exactly four distinct
memory_used values per host, with two unrelated hosts moving in identical lockstep. Host memory
does not do that.

Every read here is an explicit round trip, so the freshness of a number is a property of the
call rather than of when the connection happened to be made. `collected_at` is minted at the
same statement as the read: that is what makes it collection time and not read time, which is
the distinction a cached body has to carry to be worth anything.

note: no timeout argument. `client.api.<verb>` routes through pylxd's _APINode, which injects
the client timeout, and connection.py mounts a TimeoutHTTPAdapter that backstops it.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict

import pylxd


@dataclass(frozen=True)
class LiveRead:
    """One `/1.0/resources` body and the moment it came off the cluster."""

    body: Dict[str, Any]
    collected_at: datetime


def read_resources(client: pylxd.Client, cluster_id: str) -> LiveRead:
    """Read a cluster's resources body now, bypassing pylxd's permanent memo.

    important: the returned body is meant to be threaded through every consumer of one
    request. Calling this once per consumer triples the round trips and, worse, renders two
    different samples of memory side by side. The GPU counts already disagreed across
    endpoints once for exactly that reason.
    """
    response = client.api.resources.get()
    collected_at = datetime.now(timezone.utc)
    if response.status_code != 200:
        raise RuntimeError(
            f"Resources read for {cluster_id} returned HTTP {response.status_code}"
        )
    return LiveRead(body=response.json()["metadata"], collected_at=collected_at)
