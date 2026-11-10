"""Raw-proxy paths that reach inside a guest cost the capability for what they reach, on Incus too.

Besides exec, console, files, backups and exec logs, more paths under /1.0/instances/<name>/ reach
the guest (Incus doc/rest-api.yaml at v7.6.0). `nbd` (the root and dependent disks), `nvram` (UEFI
variables; LXD's `uefi-vars`), `port-forward` (any TCP port inside, past every network rule),
`debug/memory` (the VM's memory and its secrets) and `debug/repair` are root in the guest by another
road, so they cost `instance.exec`. `bitmaps` (read by an incremental disk export) and `sftp` (both
engines) are file disclosure and cost `instance.files`, the same as `/files` and `/backups`.
"""

import pytest

from app.core.api_key_capabilities import guest_access_capability_for


@pytest.mark.parametrize(
    "path,capability",
    [
        ("/1.0/instances/web-01/nbd", "instance.exec"),
        ("/1.0/instances/web-01/nvram", "instance.exec"),
        ("/1.0/instances/web-01/nvram/8be4df61/Boot0001", "instance.exec"),
        ("/1.0/instances/web-01/uefi-vars", "instance.exec"),
        ("/1.0/instances/web-01/port-forward", "instance.exec"),
        ("/1.0/instances/web-01/debug/memory?format=elf", "instance.exec"),
        ("/1.0/instances/web-01/debug/repair", "instance.exec"),
        ("/1.0/virtual-machines/web-01/debug/memory", "instance.exec"),
        ("/1.0/instances/web-01/bitmaps", "instance.files"),
        ("/1.0/instances/web-01/sftp", "instance.files"),
        ("/1.0/%2569nstances/web-01/sftp", "instance.files"),
        # Templates in the metadata area are rendered into the guest as root on create, copy and
        # start, so writing one is writing a guest file.
        ("/1.0/instances/web-01/metadata", "instance.files"),
        ("/1.0/instances/web-01/metadata/templates?path=hostname.tpl", "instance.files"),
    ],
)
def test_a_guest_reaching_path_costs_its_capability(path, capability):
    assert guest_access_capability_for(path) == capability


@pytest.mark.parametrize(
    "path",
    [
        # Managing the instance from outside stays at the ordinary grant.
        "/1.0/instances/web-01",
        "/1.0/instances/web-01/state",
        "/1.0/instances/web-01/snapshots",
        "/1.0/instances/web-01/access",
        # A name that merely contains a guarded word is not the guarded action.
        "/1.0/instances/nbd-server/state",
        "/1.0/instances/debug",
    ],
)
def test_managing_an_instance_from_outside_costs_nothing_extra(path):
    assert guest_access_capability_for(path) is None
