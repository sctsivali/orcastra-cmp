"""Payloads for the hardware allowlist.

DUAL_SOCKET_HOST_RESOURCES is the poison pill: every asset identifier LXD can emit is present
and carries a LEAK sentinel, because a stripper can only be tested against a payload that has
something to strip. The sentinels are distinctive strings so an assertion cannot pass by
coincidence with a real value.

NESTED_CONTAINER_RESOURCES is the opposite case, taken from what a nested container actually
reports: no `system` section at all, no storage, and sockets whose threads carry no NUMA node.
"""

from typing import Any, Dict, List

# Every one of these must be absent from anything the backend serializes.
LEAK_SENTINELS: List[str] = [
    "LEAK-SYSTEM-UUID",
    "LEAK-SYSTEM-SERIAL",
    "LEAK-SYSTEM-SKU",
    "LEAK-CHASSIS-SERIAL",
    "LEAK-BOARD-SERIAL",
    "LEAK-NIC-MAC",
    "LEAK-DISK-SERIAL",
    "LEAK-DISK-WWN",
    "LEAK-DISK-DEVICEID",
    "LEAK-DISK-DEVPATH",
    "LEAK-PARTITION-ID",
    "LEAK-PCI-ADDRESS",
    "LEAK-USB-SERIAL",
]


def _socket(number: int, numa: int) -> Dict[str, Any]:
    """One 12-core, 24-thread socket pinned to a NUMA node."""
    return {
        "socket": number,
        "name": "Intel(R) Xeon(R) Gold 6248 CPU @ 2.50GHz",
        "vendor": "GenuineIntel",
        "frequency": 2500,
        "frequency_turbo": 3900,
        "cores": [
            {"core": c, "threads": [{"numa_node": numa}, {"numa_node": numa}]}
            for c in range(12)
        ],
    }


DUAL_SOCKET_HOST_RESOURCES: Dict[str, Any] = {
    "system": {
        "uuid": "LEAK-SYSTEM-UUID",
        "serial": "LEAK-SYSTEM-SERIAL",
        "sku": "LEAK-SYSTEM-SKU",
        "vendor": "Dell Inc.",
        "product": "PowerEdge R650",
        "family": "PowerEdge",
        "version": "01",
        "type": "physical",
        "firmware": {"vendor": "Dell Inc.", "version": "2.10.2", "date": "03/15/2024"},
        "chassis": {
            "vendor": "Dell Inc.",
            "type": "Rack Mount Chassis",
            "serial": "LEAK-CHASSIS-SERIAL",
            "version": "A00",
        },
        "motherboard": {
            "vendor": "Dell Inc.",
            "product": "0X2R63",
            "serial": "LEAK-BOARD-SERIAL",
            "version": "A02",
        },
    },
    "cpu": {"architecture": "x86_64", "total": 48, "sockets": [_socket(0, 0), _socket(1, 1)]},
    "memory": {
        "total": 274877906944,
        "used": 68719476736,
        "hugepages_total": 0,
        "hugepages_used": 0,
        "hugepages_size": 2097152,
    },
    "network": {
        "cards": [
            {
                "vendor": "Intel Corporation",
                "vendor_id": "8086",
                "product": "I350 Gigabit Network Connection",
                "product_id": "1521",
                "driver": "igb",
                "driver_version": "5.6.0-k",
                "firmware_version": "1.63, 0x80000cbc",
                "pci_address": "0000:19:00.0",
                "numa_node": 0,
                "ports": [
                    {
                        "id": "eno1",
                        "address": "LEAK-NIC-MAC",
                        "port": 0,
                        "protocol": "ethernet",
                        "port_type": "twisted pair",
                        "transceiver_type": "internal",
                        "auto_negotiation": True,
                        "link_detected": True,
                        "link_speed": 1000,
                        "link_duplex": "full",
                        "infiniband": {"guid": "LEAK-NIC-MAC"},
                    }
                ],
            }
        ]
    },
    "storage": {
        "disks": [
            {
                "id": "nvme0n1",
                "device": "259:0",
                "model": "SAMSUNG MZQL21T9HCJR-00A07",
                "type": "nvme",
                "size": 1920383410176,
                "serial": "LEAK-DISK-SERIAL",
                "wwn": "LEAK-DISK-WWN",
                "device_id": "LEAK-DISK-DEVICEID",
                "device_path": "LEAK-DISK-DEVPATH",
                "removable": False,
                "read_only": False,
                "firmware_version": "GDC5302Q",
                "block_size": 512,
                "rpm": 0,
                "numa_node": 0,
                "partitions": [
                    {"id": "LEAK-PARTITION-ID", "device": "259:1", "partition": 1, "size": 1024},
                    {"id": "LEAK-PARTITION-ID", "device": "259:2", "partition": 2, "size": 2048},
                    {"id": "LEAK-PARTITION-ID", "device": "259:3", "partition": 3, "size": 4096},
                ],
            },
            {
                # A spinning disk. Under the `rotational` branch this replaced it was reported
                # as an SSD, because LXD emits no such key.
                "id": "sda",
                "device": "8:0",
                "model": "WDC WD4003FRYZ-01F0DB0",
                "type": "sata",
                "size": 4000787030016,
                "serial": "LEAK-DISK-SERIAL",
                "rpm": 7200,
                "partitions": [],
            },
        ]
    },
    "pci": {"devices": [{"vendor": "Intel", "pci_address": "LEAK-PCI-ADDRESS"}]},
    "usb": {"devices": [{"vendor": "Generic", "serial": "LEAK-USB-SERIAL"}]},
}


NESTED_CONTAINER_RESOURCES: Dict[str, Any] = {
    # No `system` block: a nested container reads no DMI.
    "cpu": {
        "architecture": "x86_64",
        "total": 4,
        "sockets": [
            {
                "socket": 0,
                "name": "AMD EPYC 7763",
                "vendor": "AuthenticAMD",
                # No numa_node on any thread.
                "cores": [{"core": c, "threads": [{}]} for c in range(4)],
            }
        ],
    },
    "memory": {"total": 8589934592, "used": 2147483648},
    "network": {"cards": []},
}


def _member(product: str, vendor: str = "Dell Inc.", arch: str = "x86_64") -> Dict[str, Any]:
    """One MicroCloud member reporting a given machine."""
    return {
        "system": {
            "vendor": vendor,
            "product": product,
            "family": "PowerEdge",
            "firmware": {"vendor": vendor, "version": "2.10.2"},
            "motherboard": {"vendor": vendor, "product": "0X2R63"},
        },
        "cpu": {"architecture": arch, "total": 48, "sockets": [_socket(0, 0), _socket(1, 1)]},
        "memory": {"total": 274877906944, "used": 68719476736},
        "storage": {
            "disks": [
                {"id": "nvme0n1", "model": "SAMSUNG MZQL2", "type": "nvme", "size": 1, "rpm": 0}
            ]
        },
    }


HOMOGENEOUS_MICROCLOUD_MEMBERS: List[Dict[str, Any]] = [_member("PowerEdge R650") for _ in range(3)]

MIXED_MICROCLOUD_MEMBERS: List[Dict[str, Any]] = [
    _member("PowerEdge R650"),
    _member("PowerEdge R650"),
    _member("AS -2124BT-HNTR", vendor="Supermicro", arch="aarch64"),
]
