"""Unit tests for ResourceManager._normalize_hardware (Server page hardware detail).

Covers the System & Firmware block plus the storage/network/GPU detail fields that the
Server page surfaces, and the defensive behaviour when sections are absent.
"""

from app.services.lxd.resources import ResourceManager

SAMPLE = {
    "cpu": {
        "architecture": "x86_64",
        "total": 16,
        "sockets": [
            {
                "socket": 0, "name": "Xeon", "vendor": "Intel",
                "frequency": 2400, "frequency_turbo": 3200,
                "cores": [
                    {"threads": [{"numa_node": 0}, {"numa_node": 0}]},
                    {"threads": [{"numa_node": 0}]},
                ],
            }
        ],
    },
    "memory": {"total": 1000, "used": 400, "hugepages_total": 0},
    "gpu": {"cards": [{
        "vendor": "Red Hat", "vendor_id": "1af4", "product": "Virtio GPU",
        "product_id": "1050", "driver": "virtio-pci", "driver_version": "1",
        "pci_address": "0000:04:00.0", "numa_node": 0,
        "nvidia": {"model": "X", "cuda_version": "12", "nvrm_version": "550"},
    }]},
    "network": {"cards": [{
        "vendor": "Red Hat", "product": "Virtio NIC", "driver": "virtio_net",
        "pci_address": "0000:05:00.0",
        "ports": [{
            "id": "enp5s0", "address": "00:16:3e:d4:fa:95", "link_detected": True,
            "link_speed_mbit": 1000, "link_duplex": "full", "auto_negotiation": True,
            "port_type": "twisted pair",
        }],
    }]},
    "storage": {"disks": [{
        "id": "sda", "device": "/dev/sda", "model": "QEMU HARDDISK", "type": "scsi",
        "size": 1024, "serial": "abc", "firmware_version": "2.5", "block_size": 512,
        "partitions": [{"id": "sda1"}, {"id": "sda2"}], "read_only": False, "removable": False,
    }]},
    "system": {
        "uuid": "u", "vendor": "QEMU", "product": "Standard PC", "family": "f",
        "version": "1.0", "sku": "s", "serial": "123", "type": "physical",
        "firmware": {"vendor": "SeaBIOS", "date": "2024", "version": "1.16"},
        "chassis": {"vendor": "QEMU", "type": "Other"},
        "motherboard": {"vendor": "QEMU", "product": "i440FX", "serial": "mb1"},
    },
}


def test_system_block_parsed():
    hw = ResourceManager._normalize_hardware(SAMPLE)
    assert hw["system"]["vendor"] == "QEMU"
    assert hw["system"]["serial"] == "123"
    assert hw["system"]["firmware"]["version"] == "1.16"
    assert hw["system"]["motherboard"]["product"] == "i440FX"
    assert hw["system"]["chassis"]["type"] == "Other"


def test_storage_detail_fields():
    hw = ResourceManager._normalize_hardware(SAMPLE)
    disk = hw["storage_devices"][0]
    assert disk["serial"] == "abc"
    assert disk["firmware_version"] == "2.5"
    assert disk["block_size"] == 512
    assert disk["partitions"] == 2


def test_network_port_detail():
    hw = ResourceManager._normalize_hardware(SAMPLE)
    port = hw["networks"][0]["ports"][0]
    assert port["link_speed"] == 1000
    assert port["link_duplex"] == "full"
    assert port["auto_negotiation"] is True
    assert port["port_type"] == "twisted pair"


def test_gpu_detail():
    hw = ResourceManager._normalize_hardware(SAMPLE)
    gpu = hw["gpu_cards"][0]
    assert gpu["vendor_id"] == "1af4"
    assert gpu["nvidia"]["cuda_version"] == "12"


def test_cpu_topology():
    hw = ResourceManager._normalize_hardware(SAMPLE)
    assert hw["cpu"]["cores"] == 2
    assert hw["cpu"]["threads"] == 3
    assert hw["cpu"]["numa_nodes"] == 1


def test_absent_sections_are_safe():
    hw = ResourceManager._normalize_hardware({})
    assert hw["system"] is None
    assert hw["storage_devices"] == []
    assert hw["networks"] == []
    assert hw["gpu_cards"] == []
    assert hw["cpu"]["cores"] == 0


if __name__ == "__main__":
    test_system_block_parsed()
    test_storage_detail_fields()
    test_network_port_detail()
    test_gpu_detail()
    test_cpu_topology()
    test_absent_sections_are_safe()
    print("normalize_hardware tests passed")
