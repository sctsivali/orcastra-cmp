"""
Detected hardware for one node, through an explicit allowlist.

GET /1.0/resources carries asset identifiers next to the facts worth showing: the system UUID
and serial, chassis and motherboard serials, a serial and WWN and device path per disk, and a
MAC address per NIC port. The Technologies page has no role gate and is reachable by Tenant, so
this payload is broadly visible and none of that may leave the backend.

Allowlist, not denylist. Every field is named here and copied into a Pydantic model declared
`extra="forbid"`, so a future LXD release that adds a new identifier is invisible by default
rather than shipped the day a cluster upgrades. Same decision, and the same shape, as
routers/integrations.py:_public_location_data.

The functions take a plain dict rather than a pylxd client, so the whole hardware model is
testable without a cluster, exactly as gpu.py is.
"""

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, TypeVar

from app.models.schemas import (
    ChassisInfo,
    CPUSocketInfo,
    CPUTopology,
    FirmwareInfo,
    HardwareProfile,
    MemoryTopology,
    MotherboardInfo,
    NetworkCardInfo,
    NetworkPortInfo,
    StorageDeviceInfo,
    SystemInfo,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# note: the ratchet in tests/test_hardware_identifiers_are_stripped.py walks the generated JSON
# schema for any property named here. It fails the day one of these gains a field, before a
# fixture exists to catch it.
IDENTIFIER_FIELDS: frozenset = frozenset(
    {
        "serial",
        "uuid",
        "sku",
        "wwn",
        "address",
        "device_id",
        "device_path",
        "infiniband",
        "certificate",
        "certificate_fingerprint",
        "mac",
        "hwaddr",
    }
)


def _agree(values: Iterable[Optional[T]]) -> Optional[T]:
    """Keep a value only when every contributor reported the same one.

    A MicroCloud row is a whole cluster, so reporting the first member's CPU model as the
    cluster's is one member's fact wearing the cluster's name. regions.py reached the same
    conclusion the blunt way, with `cpu_model=None,  # Too varied to aggregate`. Agreement is
    better: a homogeneous rack keeps the fact, a mixed one drops it.
    """
    distinct = {value for value in values if value is not None}
    return distinct.pop() if len(distinct) == 1 else None


def normalize_cpu_vendor(vendor: Optional[str]) -> Optional[str]:
    """Normalize a raw socket vendor string to the name an operator recognises.

    Lifted out of routers/regions.py so the region rollup and the node path cannot drift.
    Takes the string rather than the whole cpu block, so it also applies per socket.
    """
    if not vendor:
        return None
    lowered = vendor.lower()
    if "intel" in lowered:
        return "Intel"
    if "amd" in lowered or "advanced micro" in lowered:
        return "AMD"
    if "apple" in lowered:
        return "Apple"
    if "arm" in lowered:
        return "ARM"
    return vendor


def cpu_vendor_from_sockets(cpu_info: Optional[Dict[str, Any]]) -> Optional[str]:
    """The CPU vendor, when every populated socket reports the same one."""
    sockets = (cpu_info or {}).get("sockets") or []
    return _agree(normalize_cpu_vendor(socket.get("vendor")) for socket in sockets)


def cpu_model_from_sockets(cpu_info: Optional[Dict[str, Any]]) -> Optional[str]:
    """The CPU model, when every populated socket reports the same one.

    The version this replaced returned sockets[0].name, which on a mixed-socket host presented
    one socket's CPU as the machine's.
    """
    sockets = (cpu_info or {}).get("sockets") or []
    return _agree(socket.get("name") for socket in sockets)


def storage_types_from_disks(storage_block: Optional[Dict[str, Any]]) -> List[str]:
    """Classify physical disks into the bus and media classes an operator cares about.

    important: rotation is read from `rpm`, not `rotational`. LXD emits no `rotational` key, so
    the branch this replaced was dead and every SATA disk, spinning ones included, was labelled
    an SSD. rpm is 0 on solid state and absent when the node does not know, and an unknown
    disk is reported as plain SATA rather than guessed either way.
    """
    types: Set[str] = set()
    for disk in (storage_block or {}).get("disks") or []:
        disk_type = (disk.get("type") or "").lower()
        device = (disk.get("device") or "").lower()
        if disk_type == "nvme" or "nvme" in device:
            types.add("NVMe")
        elif disk_type == "sata" or "sd" in device:
            rpm = disk.get("rpm")
            if rpm is None:
                types.add("SATA")
            elif rpm > 0:
                types.add("HDD")
            else:
                types.add("SATA SSD")
        elif disk_type == "scsi":
            types.add("SCSI")
        elif disk_type == "virtio":
            types.add("VirtIO")
    return sorted(types)


# DMI strings a board ships with when the vendor never programmed the field. They are real
# values in the sense that the machine reports them, and useless in every other sense: a
# barebones NUC on the live fleet reports "Default string" as both its vendor and its product.
# Rendering that as the machine's make and model reads as a broken page, so it is treated as
# absent, which is what it means.
DMI_PLACEHOLDERS: frozenset = frozenset(
    {
        "default string",
        "to be filled by o.e.m.",
        "to be filled by o.e.m",
        "system manufacturer",
        "system product name",
        "system version",
        "system serial number",
        "not specified",
        "not applicable",
        "unknown",
        "none",
        "n/a",
        "empty",
        "chassis manufacture",
        "chassis version",
        "0123456789",
    }
)


def _meaningful(value: Optional[str]) -> Optional[str]:
    """Drop a DMI placeholder, keep anything a human wrote."""
    if not value:
        return None
    return None if value.strip().lower() in DMI_PLACEHOLDERS else value


def _build_system(raw: Optional[Dict[str, Any]]) -> Optional[SystemInfo]:
    """The machine's own identity, minus anything that names the individual unit.

    Returns None rather than a shell of empty fields. A nested container reports no DMI at all,
    and eight blank rows read as a detection failure on hardware that plainly has a motherboard.
    """
    if not raw:
        return None

    firmware_raw = raw.get("firmware") or {}
    chassis_raw = raw.get("chassis") or {}
    motherboard_raw = raw.get("motherboard") or {}

    system = SystemInfo(
        vendor=_meaningful(raw.get("vendor")),
        product=_meaningful(raw.get("product")),
        family=_meaningful(raw.get("family")),
        version=_meaningful(raw.get("version")),
        type=_meaningful(raw.get("type")),
        firmware=FirmwareInfo(
            vendor=_meaningful(firmware_raw.get("vendor")),
            version=_meaningful(firmware_raw.get("version")),
            date=_meaningful(firmware_raw.get("date")),
        )
        if firmware_raw
        else None,
        chassis=ChassisInfo(
            vendor=_meaningful(chassis_raw.get("vendor")),
            type=_meaningful(chassis_raw.get("type")),
            version=_meaningful(chassis_raw.get("version")),
        )
        if chassis_raw
        else None,
        motherboard=MotherboardInfo(
            vendor=_meaningful(motherboard_raw.get("vendor")),
            product=_meaningful(motherboard_raw.get("product")),
            version=_meaningful(motherboard_raw.get("version")),
        )
        if motherboard_raw
        else None,
    )

    # A sub-block whose every field was a placeholder is not a firmware section, it is three
    # empty rows under a heading.
    for name in ("firmware", "chassis", "motherboard"):
        block = getattr(system, name)
        if block is not None and not block.model_dump(exclude_none=True):
            setattr(system, name, None)

    # Everything that survived the allowlist was empty, so there is nothing to show.
    if not system.model_dump(exclude_none=True):
        return None
    return system


def _build_cpu(raw: Optional[Dict[str, Any]], member: Optional[str]) -> CPUTopology:
    """Walk sockets, cores and threads.

    important: cpu.total is a logical CPU count, not a core count, and a node can report the
    total with no socket topology at all (a Jetson does exactly that). Cores and threads stay
    at zero in that case rather than borrowing the total, which would claim a topology the node
    never described.
    """
    raw = raw or {}
    sockets_raw = raw.get("sockets") or []

    sockets: List[CPUSocketInfo] = []
    numa: Set[int] = set()
    total_cores = 0
    total_threads = 0

    for socket_raw in sockets_raw:
        cores_raw = socket_raw.get("cores") or []
        threads = 0
        for core in cores_raw:
            core_threads = core.get("threads") or []
            threads += len(core_threads)
            for thread in core_threads:
                numa_node = thread.get("numa_node")
                if numa_node is not None:
                    numa.add(numa_node)
        total_cores += len(cores_raw)
        total_threads += threads
        sockets.append(
            CPUSocketInfo(
                socket=socket_raw.get("socket"),
                name=socket_raw.get("name"),
                vendor=normalize_cpu_vendor(socket_raw.get("vendor")),
                cores=len(cores_raw),
                threads=threads,
                frequency=socket_raw.get("frequency"),
                frequency_turbo=socket_raw.get("frequency_turbo"),
                member=member,
            )
        )

    architecture = raw.get("architecture")
    return CPUTopology(
        architectures=[architecture] if architecture else [],
        total=raw.get("total") or 0,
        cores=total_cores,
        threads=total_threads,
        numa_nodes=len(numa),
        sockets=sockets,
    )


def _build_memory(raw: Optional[Dict[str, Any]]) -> MemoryTopology:
    raw = raw or {}
    total = raw.get("total") or 0
    used = raw.get("used") or 0
    return MemoryTopology(
        total=total,
        used=used,
        # note: floored. A node sampling total and used at different moments can report used
        # above total, and a negative "free" is worse than a zero.
        free=max(0, total - used),
        hugepages_total=raw.get("hugepages_total") or 0,
        hugepages_used=raw.get("hugepages_used") or 0,
        hugepages_size=raw.get("hugepages_size") or 0,
    )


def _build_networks(raw: Optional[Dict[str, Any]], member: Optional[str]) -> List[NetworkCardInfo]:
    cards: List[NetworkCardInfo] = []
    for card in (raw or {}).get("cards") or []:
        ports = [
            NetworkPortInfo(
                id=port.get("id"),
                port=port.get("port"),
                protocol=port.get("protocol"),
                port_type=port.get("port_type"),
                transceiver_type=port.get("transceiver_type"),
                auto_negotiation=port.get("auto_negotiation"),
                link_detected=port.get("link_detected"),
                # note: Incus spells this link_speed_mbit. Accept both, cost nothing.
                link_speed=port.get("link_speed_mbit") or port.get("link_speed"),
                link_duplex=port.get("link_duplex"),
            )
            for port in card.get("ports") or []
        ]
        cards.append(
            NetworkCardInfo(
                vendor=card.get("vendor"),
                vendor_id=card.get("vendor_id"),
                product=card.get("product"),
                product_id=card.get("product_id"),
                driver=card.get("driver"),
                driver_version=card.get("driver_version"),
                firmware_version=card.get("firmware_version"),
                pci_address=card.get("pci_address"),
                numa_node=card.get("numa_node"),
                ports=ports,
                member=member,
            )
        )
    return cards


def _build_storage(raw: Optional[Dict[str, Any]], member: Optional[str]) -> List[StorageDeviceInfo]:
    devices: List[StorageDeviceInfo] = []
    for disk in (raw or {}).get("disks") or []:
        devices.append(
            StorageDeviceInfo(
                id=disk.get("id"),
                device=disk.get("device"),
                model=disk.get("model"),
                type=disk.get("type"),
                size=disk.get("size") or 0,
                numa_node=disk.get("numa_node"),
                removable=disk.get("removable"),
                read_only=disk.get("read_only"),
                firmware_version=disk.get("firmware_version"),
                block_size=disk.get("block_size"),
                rpm=disk.get("rpm"),
                # note: a count, not the table. Every partition entry carries its own device
                # name and id, and the count is the only part an operator reads here anyway.
                partitions=len(disk.get("partitions") or []),
                member=member,
            )
        )
    return devices


def build_hardware_profile(
    resources_metadata: Optional[Dict[str, Any]],
    member: Optional[str] = None,
) -> HardwareProfile:
    """Detected hardware for one host, with every asset identifier left behind.

    `resources_metadata` is a raw LXD /1.0/resources body (client.resources, or the
    ?target=<member> metadata). `member` stamps device rows so a folded cluster can still say
    which host a disk belongs to.
    """
    if not resources_metadata:
        return HardwareProfile(detected=False)

    try:
        cpu = _build_cpu(resources_metadata.get("cpu"), member)
        memory = _build_memory(resources_metadata.get("memory"))
        storage_raw = resources_metadata.get("storage")
        profile = HardwareProfile(
            scope="node",
            member_count=1,
            system=_build_system(resources_metadata.get("system")),
            cpu=cpu,
            memory=memory,
            networks=_build_networks(resources_metadata.get("network"), member),
            storage_devices=_build_storage(storage_raw, member),
            storage_types=storage_types_from_disks(storage_raw),
        )
    except Exception as err:
        # important: a hardware read is decoration on a page whose numbers come from elsewhere.
        # A malformed section must never cost the caller its CPU and memory totals.
        logger.warning(f"Could not normalize hardware for {member or 'node'}: {err}")
        return HardwareProfile(detected=False)

    profile.detected = bool(
        profile.system
        or profile.cpu.total
        or profile.cpu.sockets
        or profile.memory.total
        or profile.networks
        or profile.storage_devices
    )
    return profile


def merge_hardware_profiles(profiles: Sequence[HardwareProfile]) -> HardwareProfile:
    """Fold per-member profiles into one cluster-wide profile.

    Capacity adds up. Identity does not: a singular fact survives only when every member agrees
    on it, so a rack of identical machines still names its model while a mixed cluster says
    nothing rather than presenting one member's box as the cluster's. Device rows keep the
    member label they were stamped with, which is what stops twenty-four disks reading as one
    machine's.
    """
    usable = [profile for profile in profiles if profile and profile.detected]
    if not usable:
        return HardwareProfile(detected=False)
    if len(usable) == 1:
        return usable[0]

    systems = [profile.system for profile in usable]
    folded_system = SystemInfo(
        vendor=_agree(s.vendor if s else None for s in systems),
        product=_agree(s.product if s else None for s in systems),
        family=_agree(s.family if s else None for s in systems),
        version=_agree(s.version if s else None for s in systems),
        type=_agree(s.type if s else None for s in systems),
        # note: firmware, chassis and motherboard are per-unit facts. Folding them across
        # members would put one machine's BIOS version on the whole cluster.
        firmware=None,
        chassis=None,
        motherboard=None,
    )
    if not folded_system.model_dump(exclude_none=True):
        folded_system = None

    sockets: List[CPUSocketInfo] = []
    seen_sockets = set()
    for profile in usable:
        for socket in profile.cpu.sockets:
            # Socket index means nothing cluster-wide, so drop it and dedupe on the CPU itself.
            key = (socket.name, socket.vendor, socket.cores, socket.threads, socket.frequency)
            if key in seen_sockets:
                continue
            seen_sockets.add(key)
            sockets.append(socket.model_copy(update={"socket": None}))

    architectures = sorted({arch for profile in usable for arch in profile.cpu.architectures})
    total_memory = sum(profile.memory.total for profile in usable)
    used_memory = sum(profile.memory.used for profile in usable)

    return HardwareProfile(
        scope="cluster",
        member_count=len(usable),
        detected=True,
        system=folded_system,
        cpu=CPUTopology(
            architectures=architectures,
            total=sum(profile.cpu.total for profile in usable),
            cores=sum(profile.cpu.cores for profile in usable),
            threads=sum(profile.cpu.threads for profile in usable),
            numa_nodes=sum(profile.cpu.numa_nodes for profile in usable),
            sockets=sockets,
        ),
        memory=MemoryTopology(
            total=total_memory,
            used=used_memory,
            free=max(0, total_memory - used_memory),
            hugepages_total=sum(profile.memory.hugepages_total for profile in usable),
            hugepages_used=sum(profile.memory.hugepages_used for profile in usable),
            hugepages_size=_agree(profile.memory.hugepages_size for profile in usable) or 0,
        ),
        networks=[card for profile in usable for card in profile.networks],
        storage_devices=[device for profile in usable for device in profile.storage_devices],
        storage_types=sorted({t for profile in usable for t in profile.storage_types}),
    )
