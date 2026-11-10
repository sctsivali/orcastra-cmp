"""What a delivery is allowed to contain.

An event lands on infrastructure a third party runs, so the payload comes from a typed object
with named fields, never a dict or `**kwargs` passthrough. Never included:

  * anything from inside a guest (exec output, files, screenshots, clipboard, console buffer).
  * IP and MAC addresses and network device configuration: a co-tenant's topology.
  * an instance's `config` and `devices` maps: `user.*` metadata and cloud-init carry credentials.
  * certificates and fingerprints; API key ids, secrets, hashes, Vault paths, callback URLs.
  * hardware and resource figures; the pull API checks the caller's grant per request.
  * anything about another organization sharing the same cluster.

`resource_name` is included. A tenant chooses it and it can identify them, so fan-out is filtered
by project grant before a delivery is created.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

SPEC_VERSION = "1.0"

# Every field a `data` block may carry. The builder refuses anything else, so a new field has to
# be added here.
ALLOWED_DATA_FIELDS = frozenset(
    {"cluster_id", "project", "resource_type", "resource_name", "state", "operation_id"}
)


@dataclass(frozen=True)
class ResourceEvent:
    """A lifecycle event about one resource. The only shape a delivery is built from."""

    event_id: str
    event_type: str
    occurred_at: str
    cluster_id: str
    resource_type: str
    resource_name: str
    project: Optional[str] = None
    state: Optional[str] = None
    operation_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def data(self) -> Dict[str, Any]:
        payload = {
            "cluster_id": self.cluster_id,
            "project": self.project,
            "resource_type": self.resource_type,
            "resource_name": self.resource_name,
            "state": self.state,
            "operation_id": self.operation_id,
        }
        # `extra` lets an event add a field without a new dataclass; only ALLOWED_DATA_FIELDS keys
        # pass, anything else is dropped.
        for key, value in self.extra.items():
            if key in ALLOWED_DATA_FIELDS:
                payload[key] = value
        return {k: v for k, v in payload.items() if v is not None}


def build_envelope(
    event: ResourceEvent,
    *,
    deployment_id: str,
    subscription_id: int,
    installation_id: int,
    sequence: int,
) -> Dict[str, Any]:
    """The document that is signed and sent.

    `id` is stable across every attempt and is the receiver's idempotency key; delivery is
    at-least-once. The caller supplies `sequence`; the dispatcher uses the enqueue time in epoch
    milliseconds, which orders deliveries roughly but is neither gap-free nor unique.
    """
    return {
        "specversion": SPEC_VERSION,
        "id": event.event_id,
        "type": event.event_type,
        "source": f"orcastra/{deployment_id}",
        "time": event.occurred_at,
        "subscription_id": subscription_id,
        "installation_id": installation_id,
        "sequence": sequence,
        "data": event.data(),
    }


def assert_payload_is_clean(payload: Dict[str, Any]) -> None:
    """A last structural check, so a change to the builder cannot quietly widen the shape.

    Raises ValueError naming the field. Runs on the enqueue path, so it guards every stored row.
    """
    data = payload.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    unknown = sorted(set(data) - ALLOWED_DATA_FIELDS)
    if unknown:
        raise ValueError(f"a delivery may not carry: {', '.join(unknown)}")
