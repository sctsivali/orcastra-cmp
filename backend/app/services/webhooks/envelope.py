"""What a delivery is allowed to contain.

An event leaves this deployment and lands on infrastructure a third party runs, so the payload is
built from a typed object with named fields. There is no dict passthrough and no `**kwargs`,
because the alternative is a rule enforced by review, and review is where this class of leak comes
from: someone adds a field that is obviously useful and not obviously identifying.

What is deliberately absent, and why each one:

  * anything derived from inside a guest: exec output, file contents, screenshots, clipboard,
    console buffer. The audit log already refuses to record these; a webhook carrying them would
    be a lower-friction exfiltration path than the API it complements.
  * IP and MAC addresses, and network device configuration. On a shared cluster these map a
    co-tenant's topology.
  * an instance's `config` and `devices` maps. They carry `user.*` metadata and cloud-init, which
    routinely contain credentials.
  * certificates and fingerprints of any kind. The proxy already refuses `/1.0/certificates` for
    exactly this reason.
  * API key ids, secrets, hashes, Vault paths, callback URLs.
  * hardware totals and resource figures. Those exist on the pull API where the caller's grant is
    checked per request; a push has no such check.
  * anything about another organization sharing the same cluster.

note: `resource_name` is included, and it is the one judgement call. An instance name is chosen by
the tenant and can encode who they are, which is why fan-out is filtered by project grant before a
delivery is created rather than after.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

SPEC_VERSION = "1.0"

# Every field a `data` block may carry. The builder refuses anything else, so a new field is a
# decision somebody makes here rather than one that arrives with a feature.
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
        # `extra` exists so a future event can add a field without a new dataclass, and it is
        # filtered rather than trusted. A caller that puts an IP address in here gets nothing.
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
    at-least-once and the documentation says so rather than implying otherwise. `sequence` is
    monotonic per subscription, so a gap tells a receiver something was dropped rather than
    leaving it to infer silence.
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
    """A last structural check, so a future builder cannot quietly widen the shape.

    Raises ValueError naming the field. Called on the enqueue path rather than only in a test,
    because a test protects the code that exists today and this protects the row.
    """
    data = payload.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    unknown = sorted(set(data) - ALLOWED_DATA_FIELDS)
    if unknown:
        raise ValueError(f"a delivery may not carry: {', '.join(unknown)}")
