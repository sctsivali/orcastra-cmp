"""Raw-proxy paths that reach inside a guest cost the capability for what they reach, on Incus too.

The guard listed LXD's guest-reaching paths only (exec, console, files, backups, exec logs).
Incus serves more under /1.0/instances/<name>/ (doc/rest-api.yaml at v7.6.0), and two shared
paths were missed on both engines, so a key with nothing but `read`/`write` and `lxd.proxy` could:

- `nbd`            open an NBD connection to the instance's root and dependent disks
- `nvram`          read and rewrite its UEFI variables (Incus; LXD calls it `uefi-vars`)
- `port-forward`   connect to any TCP port inside the instance, past every network rule
- `debug/memory`   dump the VM's memory, which holds whatever secrets the guest has
- `debug/repair`   run an internal repair action on it
- `bitmaps`        create the dirty bitmaps an incremental disk export reads
- `sftp`           (LXD and Incus) speak SFTP to the guest filesystem

A disk, memory or boot-variable reach is root in the guest by another road, so it costs
`instance.exec`; SFTP and bitmaps are file disclosure and cost `instance.files`, the same as
`/files` and `/backups`.
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
