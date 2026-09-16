"""One reading of `limits.cpu` and one way to sum what a member has allocated.

note: "allocated" is the sum of the running instances' CPU limits, which is what the
member, node and project reads call "used". LXD's resources API carries no host utilisation
figure and nothing here reads /1.0/metrics. A cluster may legitimately allocate more vCPUs
than it has threads (KVM shares them), so a ratio above 1 is a fact to present, not a fault.

The grammar follows LXD's own `limits.cpu`: a count ("4"), a range ("0-3"), a set ("0,2"),
or a union of both ("0-3,8-11"), duplicates counted once. Anything LXD would refuse to store
(a reversed range, a fraction, text) falls back to the caller's default rather than inventing
capacity. Allocation reads `expanded_config`, which is what the instance runs with once its
profiles are applied; the instance-local `config` alone counted a profile-inherited limit as
one core.
"""

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

STANDALONE_LOCATIONS = frozenset({"", "none"})


def parse_cpu_limit(value: object, default: int = 1) -> int:
    """Cores a `limits.cpu` value pins or counts. `default` for unset or unparseable."""
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    if text.isdigit():
        count = int(text)
        return count if count > 0 else default
    ids: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            ids.add(int(part))
            continue
        low, sep, high = part.partition("-")
        if not sep or not low.isdigit() or not high.isdigit():
            logger.debug("Unparseable limits.cpu %r, counting %d", value, default)
            return default
        lo, hi = int(low), int(high)
        if hi < lo:
            logger.debug("Reversed limits.cpu range %r, counting %d", value, default)
            return default
        ids.update(range(lo, hi + 1))
    return len(ids) if ids else default


def instance_cpu_allocation(instance: Any, default: int = 1) -> int:
    """What this instance runs with: the expanded config first, the local config as fallback."""
    expanded = getattr(instance, "expanded_config", None) or {}
    config = getattr(instance, "config", None) or {}
    value = expanded.get("limits.cpu") if isinstance(expanded, dict) else None
    if value is None and isinstance(config, dict):
        value = config.get("limits.cpu")
    return parse_cpu_limit(value, default=default)


@dataclass(frozen=True)
class CpuAllocation:
    """Instances placed on a member (or a standalone host) and the cores they allocate."""

    instances: int
    running: int
    cores: int


def allocated_cpu(
    instances: Iterable[Any], *, member: Optional[str], running_only: bool = True
) -> CpuAllocation:
    """Sum the CPU limits of the instances on `member`; `member=None` means a standalone host.

    A standalone host's instances carry an empty or "none" location, and a member filter must
    never match them; a clustered read must never sum the other members' instances.
    """
    count = running = cores = 0
    for instance in instances:
        location = getattr(instance, "location", "") or ""
        if member is not None and location != member:
            continue
        count += 1
        is_running = getattr(instance, "status", "") == "Running"
        if is_running:
            running += 1
        if is_running or not running_only:
            cores += instance_cpu_allocation(instance)
    return CpuAllocation(instances=count, running=running, cores=cores)


def allocation_ratio(allocated: int, threads: int) -> float:
    """Allocated vCPUs per hardware thread, 0.0 when the thread count is unknown."""
    if threads <= 0:
        return 0.0
    return round(allocated / threads, 2)


def clamp_available(threads: int, allocated: int) -> int:
    """Threads not yet allocated; zero, never negative, once the member is over-committed."""
    return max(0, threads - allocated)
