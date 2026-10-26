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
from typing import Any, Iterable, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


# A CPU id above this is not a CPU set, it is a value built to make the parser allocate.
_MAX_CPU_ID = 65535


def limits_set(
    instance_type: Optional[str], config: Mapping[str, Any], *, complete: bool = True
) -> Tuple[Optional[bool], Optional[bool]]:
    """Whether an instance's own config caps its CPU and its memory.

    A virtual machine's size is its cap, so both are True. A container without `limits.cpu` is
    measured against its host's threads and LXD reports its memory against the host's, so those
    figures are the host's size, not a limit the instance was given. `complete` is False when
    `config` is the local config alone and a profile could still set the key: an absent key is
    then unknown (None) rather than unset.
    """
    if instance_type == "virtual-machine":
        return True, True

    def known(present: bool) -> Optional[bool]:
        return True if present else (False if complete else None)

    cpu = parse_cpu_limit(config.get("limits.cpu"), default=0) > 0
    memory = bool(str(config.get("limits.memory") or "").strip())
    return known(cpu), known(memory)


def parse_cpu_limit(value: object, default: int = 1) -> int:
    """Cores a `limits.cpu` value pins or counts. `default` for unset or unparseable.

    note: counted on merged intervals, never by materialising every id. "0-4000000000" used to
    build a set of four billion ints, and the monitoring sweep now parses every running
    instance's limit on each collection.
    """
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    # note: isdigit() accepts "²" and other non-ASCII digits that int() refuses, and int()
    # refuses more than 4300 digits; neither is a CPU set LXD would store.
    if not text.isascii() or len(text) > 4096:
        return default
    if text.isdigit():
        count = int(text)
        return count if 0 < count <= _MAX_CPU_ID + 1 else default
    intervals: list[tuple[int, int]] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            lo = hi = int(part)
        else:
            low, sep, high = part.partition("-")
            if not sep or not low.isdigit() or not high.isdigit():
                logger.debug("Unparseable limits.cpu %r, counting %d", value, default)
                return default
            lo, hi = int(low), int(high)
            if hi < lo:
                logger.debug("Reversed limits.cpu range %r, counting %d", value, default)
                return default
        if hi > _MAX_CPU_ID:
            logger.debug("Out-of-range limits.cpu %r, counting %d", value, default)
            return default
        intervals.append((lo, hi))
    if not intervals:
        return default
    intervals.sort()
    count = 0
    current_lo, current_hi = intervals[0]
    for lo, hi in intervals[1:]:
        if lo <= current_hi + 1:
            current_hi = max(current_hi, hi)
            continue
        count += current_hi - current_lo + 1
        current_lo, current_hi = lo, hi
    count += current_hi - current_lo + 1
    return count


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


def effective_vcpus(
    instance: Any,
    *,
    reported: Optional[float] = None,
    host_threads: Optional[int] = None,
) -> Optional[int]:
    """vCPUs a live CPU percentage is measured against, or None when nothing says.

    important: not instance_cpu_allocation. That one is allocation accounting and counts an unset
    limit as one core, which for a container without `limits.cpu` would read a busy
    eight-thread container as 800 percent of "1 vCPU". Order: LXD's own effective count (it
    reports 0 for virtual machines, so 0 falls through), then `limits.cpu`, then LXD's defaults:
    one vCPU for a virtual machine, every host thread for an unlimited container.
    """
    if reported is not None and reported > 0:
        return max(1, int(round(reported)))
    expanded = _field(instance, "expanded_config") or {}
    config = _field(instance, "config") or {}
    value = expanded.get("limits.cpu") if isinstance(expanded, dict) else None
    if value is None and isinstance(config, dict):
        value = config.get("limits.cpu")
    count = parse_cpu_limit(value, default=0)
    if count > 0:
        return count
    if _field(instance, "type") == "virtual-machine":
        return 1
    if host_threads is not None and host_threads > 0:
        return host_threads
    return None


def _field(instance: Any, name: str) -> Any:
    if isinstance(instance, dict):
        return instance.get(name)
    return getattr(instance, name, None)
