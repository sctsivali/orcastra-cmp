"""
Cloud-init costs instance.exec by what it changes, on every write path and every door, and a '*' key
keeps its host-reaching config.

Each case sends one request through the real route (tests/_guest_gate_doors.py: raw proxy, /proxy,
MCP) and asserts the status, the capability the refusal names, the audit record, what was read and
what was written. A create carrying a cloud-init key (cloud-init.ssh-keys.<name> included) costs
exec without a read. A write to an existing instance or profile is compared with the object's own
config, read once: an unchanged value is sent, a new, changed or removed one costs exec, a failed
read sends nothing and leaves a record, and a key holding exec is never read for. A '*' key's raw.*,
privileged, nesting and host devices pass the raw proxy while /proxy keeps PROXY_PRICE. OrcaHub's
own instance, volume, snapshot and project bodies are still sent.
"""

from typing import Any, Dict, List, Tuple

import pytest
import requests

from app.mcp import tools_write
from app.services.lxd import health
from tests._cloud_init_fakes import ConfigSession
from tests._guest_gate_doors import (  # noqa: F401
    BOTH, EVERY, EXEC, FILES, LEGACY, NARROW, NO_GUEST, ORCAHUB, ORG, SPELLING_IDS, Cluster, door_fixture,
    proxy_cluster, spell,
)
from tests._limited_write_fakes import FakeClient, _no_webhook_events  # noqa: F401
from tests._raw_proxy_fakes import ORCAHUB_READS, CLUSTER_ID, ENDPOINT, _clean_breaker, fence_calls  # noqa: F401

X = "#cloud-config\npackages: [nginx]\n"
Y = "#cloud-config\nruncmd: [id]\n"
UD, VD, CUD, CVD = "user.user-data", "user.vendor-data", "cloud-init.user-data", "cloud-init.vendor-data"
SSH, SSH_DEPLOY = "cloud-init.ssh-keys.root", "cloud-init.ssh-keys.deploy"
MD = "user.meta-data"
CLOUD_INIT_KEYS = [UD, VD, CUD, CVD, SSH, SSH_DEPLOY, MD]
KEY_A = "root:ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA ops@example.test"
KEY_B = "root:ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB intruder@example.test"
FAMILIES = ["instances", "containers", "virtual-machines"]
DENIED, GRANTED = [NO_GUEST, FILES, ORCAHUB, LEGACY], [EXEC, BOTH, EVERY]
DENIED_IDS, GRANTED_IDS = ["no-guest", "files", "orcahub", "legacy"], ["exec", "both", "every"]


def _held(config: Dict[str, Any], path: str, project: str = "default") -> Cluster:
    return Cluster(ConfigSession({(project, path): {"name": path.rsplit("/", 1)[-1], "config": dict(config)}}))


def _priced(seen: Any, reads: List[Tuple[str, str]]) -> None:
    assert seen.status == 403 and "instance.exec" in seen.text, (seen.status, seen.text)
    assert seen.cluster.session.reads == reads and seen.cluster.writes == []
    assert [reason for reason, _ in seen.refusals] == ["capability_not_granted"]


def _passed(seen: Any, reads: List[Tuple[str, str]], method: str, body: Any) -> None:
    assert seen.status == 200, (seen.status, seen.text)
    assert seen.cluster.session.reads == reads and seen.cluster.writes == [(method, body)]
    assert seen.refusals == []


# Creates: priced without a read.

CREATES = [
    *[("POST", f"/1.0/{family}", {"name": "c1", "source": {"type": "image", "alias": "ubuntu/24.04"}})
      for family in FAMILIES],
    ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "web-1"}}),
    ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "none"}}),
    ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "migration", "mode": "pull",
                                                          "operation": "https://x/1.0/operations/1"}}),
    ("POST", "/1.0/profiles", {"name": "p9"}),
    ("POST", "/1.0/%69nstances/", {"name": "c1"}),
    ("POST", "/1.0/PROFILES?x=1", {"name": "p9"}),
]
CREATE_IDS = [*FAMILIES, "copy", "none", "migration", "profile", "encoded-slash", "profile-upper-query"]
CREATE_VALUES = [X, "", None, 1, True, [X]]


@pytest.mark.parametrize("value", CREATE_VALUES, ids=repr)
@pytest.mark.parametrize("name", [*CLOUD_INIT_KEYS, "User.User-Data", "CLOUD-INIT.VENDOR-DATA",
                                  "Cloud-Init.SSH-Keys.Root"])
@pytest.mark.parametrize("method,path,body", CREATES, ids=CREATE_IDS)
def test_a_create_carrying_cloud_init_costs_exec_without_a_read(
    door: Any, method: str, path: str, body: Dict[str, Any], name: str, value: Any
) -> None:
    seen = door.raw(NO_GUEST, method, path, body={**body, "config": {name: value}})
    _priced(seen, [])
    assert seen.cluster.untouched


@pytest.mark.parametrize("api_key", DENIED + GRANTED, ids=DENIED_IDS + GRANTED_IDS)
@pytest.mark.parametrize("method,path,body", [CREATES[0], CREATES[4]], ids=["instances", "none"])
def test_only_a_key_holding_exec_creates_with_cloud_init(door: Any, api_key: Dict[str, Any], method: str, path: str,
                                                         body: Dict[str, Any]) -> None:
    sent_body = {**body, "config": {UD: X}}
    seen = door.raw(api_key, method, path, body=sent_body)
    if api_key in GRANTED:
        _passed(seen, [], method, sent_body)
    else:
        _priced(seen, [])


# Updates: judged against the object's own config, read once.

UPDATES = [
    *[(method, f"/1.0/{family}/web-1", f"/{family}/web-1") for family in FAMILIES for method in ("PUT", "PATCH")],
    *[("POST", f"/1.0/{family}/web-1", f"/{family}/web-1") for family in FAMILIES],
    ("PUT", "/1.0/profiles/p1", "/profiles/p1"),
    ("PATCH", "/1.0/profiles/p1", "/profiles/p1"),
]
UPDATE_IDS = [f"{m}-{t.strip('/')}" for m, _p, t in UPDATES]
# Each combo is (id, the object's own config, the config sent, priced).
COMBOS = [
    ("unchanged", {"@": X}, {"@": X}, False),
    ("unchanged-beside-a-resize", {"@": X, "limits.cpu": "2"}, {"@": X, "limits.cpu": "4"}, False),
    ("empty-on-absent", {}, {"@": ""}, False),
    ("null-on-absent", {}, {"@": None}, False),
    ("changed", {"@": Y}, {"@": X}, True),
    ("new", {"limits.cpu": "2"}, {"@": X}, True),
    ("removed-by-empty", {"@": Y}, {"@": ""}, True),
    ("removed-by-null", {"@": Y}, {"@": None}, True),
    ("number", {"@": "1"}, {"@": 1}, True),
    ("bool", {"@": "true"}, {"@": True}, True),
    ("list", {}, {"@": [X]}, True),
    ("other-spelling", {"@": X}, {"^": X}, True),
]


def _fill(config: Dict[str, Any], name: str) -> Dict[str, Any]:
    return {({"@": name, "^": name.upper()}.get(k, k)): v for k, v in config.items()}


@pytest.mark.parametrize("name", CLOUD_INIT_KEYS)
@pytest.mark.parametrize("combo,held,config,priced", COMBOS, ids=[c[0] for c in COMBOS])
@pytest.mark.parametrize("method,path,target", UPDATES, ids=UPDATE_IDS)
def test_an_update_is_priced_by_what_it_changes(
    door: Any, method: str, path: str, target: str, combo: str, held: Dict[str, Any], config: Dict[str, Any],
    priced: bool, name: str
) -> None:
    body = {"config": _fill(config, name), **({"name": "web-2"} if method == "POST" else {})}
    seen = door.raw(NO_GUEST, method, path, body=body, cluster=_held(_fill(held, name), target))
    if priced:
        _priced(seen, [(ENDPOINT + target, "default")])
    else:
        _passed(seen, [(ENDPOINT + target, "default")], method, body)


@pytest.mark.parametrize("how", SPELLING_IDS)
@pytest.mark.parametrize("method,template,target", [("PATCH", "/1.0/{instances}/web-1", "/instances/web-1"),
                                                    ("PUT", "/1.0/{virtual-machines}/vm-1", "/virtual-machines/vm-1"),
                                                    ("PATCH", "/1.0/{profiles}/p1", "/profiles/p1")],
                         ids=["instance", "vm", "profile"])
def test_every_spelling_of_an_update_reads_the_one_object(
    door: Any, how: str, method: str, template: str, target: str
) -> None:
    path, body, reads = spell(template, how), {"config": {CUD: X}}, [(ENDPOINT + target, "default")]
    _passed(door.raw(NO_GUEST, method, path, body=body, cluster=_held({CUD: X}, target)), reads, method, body)
    _priced(door.raw(NO_GUEST, method, path, body=body, cluster=_held({CUD: Y}, target)), reads)


@pytest.mark.parametrize("api_key,project", [*[(k, None) for k in DENIED], (NARROW, "tenant-a")],
                         ids=[*DENIED_IDS, "narrow"])
def test_every_key_without_exec_is_read_for_and_judged(door: Any, api_key: Dict[str, Any], project: Any) -> None:
    where = project or "default"
    body = {"config": {UD: X}}
    changed = door.raw(api_key, "PATCH", "/1.0/instances/web-1?project=elsewhere", body=body, project=project,
                       cluster=_held({UD: Y}, "/instances/web-1", where))
    _priced(changed, [(ENDPOINT + "/instances/web-1", where)])
    same = door.raw(api_key, "PATCH", "/1.0/instances/web-1", body=body, project=project,
                    cluster=_held({UD: X}, "/instances/web-1", where))
    _passed(same, [(ENDPOINT + "/instances/web-1", where)], "PATCH", body)


@pytest.mark.parametrize("method,path,target", UPDATES, ids=UPDATE_IDS)
def test_a_value_only_a_profile_gives_is_new_on_the_object(door: Any, method: str, path: str, target: str) -> None:
    session = ConfigSession({("default", target): {"config": {}, "expanded_config": {UD: X}, "profiles": ["ci"]}})
    body = {"config": {UD: X}, **({"name": "web-2"} if method == "POST" else {})}
    _priced(door.raw(NO_GUEST, method, path, body=body, cluster=Cluster(session)), [(ENDPOINT + target, "default")])


@pytest.mark.parametrize("api_key", GRANTED, ids=GRANTED_IDS)
@pytest.mark.parametrize("method,path,target", UPDATES, ids=UPDATE_IDS)
def test_a_key_holding_exec_is_never_read_for(
    door: Any, api_key: Dict[str, Any], method: str, path: str, target: str
) -> None:
    body = {"config": {UD: X}}
    _passed(door.raw(api_key, method, path, body=body, cluster=_held({UD: Y}, target)), [], method, body)


NOT_READ = [
    (None, 404), (500, 502), (503, 502), (403, 502),
    (requests.exceptions.ReadTimeout("read timed out, host 10.0.0.9"), 504),
    (requests.exceptions.ConnectTimeout("connect timed out, host 10.0.0.9"), 504),
    (requests.exceptions.ConnectionError("refused, host 10.0.0.9"), 502),
]
NOT_READ_IDS = ["missing", "500", "503", "403", "read-timeout", "connect-timeout", "unreachable"]


@pytest.mark.parametrize("fail,status", NOT_READ, ids=NOT_READ_IDS)
@pytest.mark.parametrize("method,path,target", UPDATES, ids=UPDATE_IDS)
def test_a_read_that_fails_sends_nothing_and_is_recorded(door: Any, method: str, path: str, target: str, fail: Any,
                                                         status: int) -> None:
    held = {} if fail is None else {("default", target): {"config": {UD: X}}}
    session = ConfigSession(held, fail=fail)
    seen = door.raw(NO_GUEST, method, path, body={"config": {UD: X}}, cluster=Cluster(session))
    assert seen.status == status and "The write was not sent" in seen.text and "10.0.0.9" not in seen.text
    assert len(seen.cluster.session.reads) == 1 and seen.cluster.writes == []
    assert [reason for reason, _ in seen.refusals] == ["cloud_init_not_compared"]
    assert not health.is_tripped(CLUSTER_ID)


# /proxy and MCP.

PROXY_CREATES = [{"type": "image", "alias": "ubuntu/24.04"}, {"type": "copy", "source": "web"}, {"type": "none"}]


@pytest.mark.parametrize("name", CLOUD_INIT_KEYS)
@pytest.mark.parametrize("api_key", DENIED, ids=DENIED_IDS)
@pytest.mark.parametrize("source", PROXY_CREATES, ids=["image", "copy", "none"])
def test_proxy_create_with_cloud_init_costs_exec_with_an_attributed_record(
    door: Any, source: Dict[str, Any], api_key: Dict[str, Any], name: str
) -> None:
    seen = door.proxy(api_key, "create_instance", {"name": "c1", "source": source, "config": {name: X}})
    assert seen.status == 403 and "instance.exec" in seen.text and seen.cluster.untouched
    assert seen.refusals == [("capability_not_granted", "integration.proxy.request")]
    assert (seen.records[-1]["target_organization_id"], seen.records[-1]["target_organization_slug"]) == ORG


@pytest.mark.parametrize("name", [UD, CUD, SSH])
@pytest.mark.parametrize("combo,held,config,priced", COMBOS, ids=[c[0] for c in COMBOS])
def test_proxy_update_is_priced_by_what_it_changes(door: Any, combo: str, held: Dict[str, Any],
                                                  config: Dict[str, Any], priced: bool, name: str) -> None:
    params = {"name": "web", "config": _fill(config, name)}
    seen = door.proxy(NO_GUEST, "update_instance", params, cluster=proxy_cluster(_fill(held, name)))
    assert seen.cluster.session.reads == [(ENDPOINT + "/instances/web", "default")]
    if priced:
        assert seen.status == 403 and "instance.exec" in seen.text and seen.cluster.writes == []
        assert seen.refusals == [("capability_not_granted", "integration.proxy.request")]
        assert seen.records[-1]["target_organization_id"] == ORG[0]
    else:
        assert seen.status == 200 and seen.code is None, seen.text
        assert [(verb, name_, body["config"]) for verb, name_, body in seen.cluster.writes] == [
            ("save", "web", params["config"])]
        assert seen.refusals == []


@pytest.mark.parametrize("fail,code", [(None, "not_found"), (500, "cluster_unavailable"), (503, "cluster_unavailable"),
                                       (requests.exceptions.ReadTimeout("host 10.0.0.9"), "timeout"),
                                       (requests.exceptions.ConnectionError("host 10.0.0.9"), "cluster_unavailable")],
                         ids=["missing", "500", "503", "timeout", "unreachable"])
def test_proxy_update_sends_nothing_when_the_read_fails(door: Any, fail: Any, code: str) -> None:
    cluster = proxy_cluster(None) if fail is None else proxy_cluster({UD: X}, fail=fail)
    seen = door.proxy(NO_GUEST, "update_instance", {"name": "web", "config": {UD: X}}, cluster=cluster)
    assert seen.status == 200 and seen.code == code and "The write was not sent" in seen.text
    assert "10.0.0.9" not in seen.text and seen.cluster.writes == []
    assert seen.refusals == [("cloud_init_not_compared", "integration.proxy.request")]


class _Disconnected(Cluster):
    """A cluster whose pooled client cannot be built, as the connection manager answers None."""

    def _pooled(self, cluster_id: str) -> Any:
        self.built.append(cluster_id)
        return None


def test_proxy_update_sends_nothing_when_the_cluster_is_not_connected(door: Any) -> None:
    seen = door.proxy(NO_GUEST, "update_instance", {"name": "web", "config": {UD: X}}, cluster=_Disconnected())
    assert seen.status == 200 and seen.code == "cluster_unavailable" and "The write was not sent" in seen.text
    assert seen.cluster.session.calls == [] and seen.cluster.writes == []
    assert seen.refusals == [("cloud_init_not_compared", "integration.proxy.request")]


def test_proxy_compares_the_instances_own_config_not_its_profiles(door: Any) -> None:
    cluster = Cluster(client=FakeClient(ConfigSession({("default", "/instances/web"): {
        "name": "web", "config": {}, "expanded_config": {UD: X}, "profiles": ["ci"]}})))
    seen = door.proxy(NO_GUEST, "update_instance", {"name": "web", "config": {UD: X}}, cluster=cluster)
    assert seen.status == 403 and "instance.exec" in seen.text and seen.cluster.writes == []


def test_proxy_asks_exec_on_top_of_files_for_a_host_disk(door: Any) -> None:
    params = {"name": "web", "config": {UD: X}, "devices": {"d": {"type": "disk", "source": "/srv", "path": "/srv"}}}
    seen = door.proxy(FILES, "update_instance", params, cluster=proxy_cluster({}))
    assert seen.status == 403 and "instance.exec" in seen.text and seen.cluster.writes == []
    unchanged = door.proxy(FILES, "update_instance", params, cluster=proxy_cluster({UD: X}))
    assert unchanged.status == 200 and [verb for verb, *_ in unchanged.cluster.writes] == ["save"]


@pytest.mark.parametrize("api_key", GRANTED, ids=GRANTED_IDS)
def test_proxy_never_reads_for_a_key_holding_exec(door: Any, api_key: Dict[str, Any]) -> None:
    seen = door.proxy(api_key, "update_instance", {"name": "web", "config": {UD: X}}, cluster=proxy_cluster({UD: Y}))
    assert seen.status == 200 and seen.cluster.session.reads == [] and [v for v, *_ in seen.cluster.writes] == ["save"]


async def test_mcp_prices_cloud_init_the_same_way(door: Any) -> None:
    create = await door.mcp(tools_write, "lxd_create_instance", NO_GUEST, cluster_id=CLUSTER_ID, name="c1",
                            source={"type": "image", "alias": "a"}, config={VD: X})
    assert create.status == 0 and "instance.exec" in create.text and create.cluster.untouched
    ssh_create = await door.mcp(tools_write, "lxd_create_instance", NO_GUEST, cluster_id=CLUSTER_ID, name="c2",
                                source={"type": "image", "alias": "a"}, config={SSH: KEY_A})
    assert ssh_create.status == 0 and "instance.exec" in ssh_create.text and ssh_create.cluster.untouched
    same = await door.mcp(tools_write, "lxd_update_instance", NO_GUEST, proxy_cluster({CUD: X}), cluster_id=CLUSTER_ID,
                          name="web", config={CUD: X, "limits.memory": "4GiB"})
    assert same.status == 200 and [verb for verb, *_ in same.cluster.writes] == ["save"]
    changed = await door.mcp(tools_write, "lxd_update_instance", NO_GUEST, proxy_cluster({CUD: Y}),
                             cluster_id=CLUSTER_ID, name="web", config={CUD: X})
    assert changed.status == 0 and "instance.exec" in changed.text and changed.cluster.writes == []
    gone = await door.mcp(tools_write, "lxd_update_instance", NO_GUEST, proxy_cluster(None), cluster_id=CLUSTER_ID,
                          name="web", config={CUD: X})
    assert gone.status == 0 and gone.code == "not_found" and gone.cluster.writes == []
    assert [reason for reason, _ in door.trail.refusals] == [
        "capability_not_granted", "capability_not_granted", "capability_not_granted", "cloud_init_not_compared"]


@pytest.mark.parametrize("door_name", ["raw", "proxy", "mcp"])
async def test_an_ssh_key_is_priced_by_what_it_changes_on_every_door(door: Any, door_name: str) -> None:
    """A login key for root costs exec when it is new or changed, and a round trip carrying it unchanged is sent."""

    async def write(held: Dict[str, Any], config: Dict[str, Any]) -> Any:
        if door_name == "raw":
            return door.raw(NO_GUEST, "PUT", "/1.0/instances/web", body={"config": config},
                            cluster=_held(held, "/instances/web"))
        if door_name == "proxy":
            return door.proxy(NO_GUEST, "update_instance", {"name": "web", "config": config},
                              cluster=proxy_cluster(held))
        return await door.mcp(tools_write, "lxd_update_instance", NO_GUEST, proxy_cluster(held), cluster_id=CLUSTER_ID,
                              name="web", config=config)

    for held, config, priced in (({}, {SSH: KEY_A}, True), ({SSH: KEY_A}, {SSH: KEY_B}, True),
                                 ({SSH: KEY_A}, {SSH: KEY_A, "limits.cpu": "2"}, False)):
        seen = await write(held, config)
        assert (seen.status in (0, 403) and "instance.exec" in seen.text) is priced, (held, config, seen.text)
        assert (seen.cluster.writes == []) is priced
        assert [reason for reason, _ in seen.refusals] == (["capability_not_granted"] if priced else [])


@pytest.mark.parametrize("name", ["cloud-init.ssh-keys", "cloud-init.ssh-keysroot", "user.ssh-keys.root"])
def test_a_key_that_only_looks_like_an_ssh_key_is_not_priced(door: Any, name: str) -> None:
    body = {"name": "c1", "source": {"type": "none"}, "config": {name: KEY_A}}
    _passed(door.raw(NO_GUEST, "POST", "/1.0/instances", body=body), [], "POST", body)


# A '*' key keeps its host-reaching config.

HOST_REACH = {
    "raw.lxc": ({"raw.lxc": "lxc.apparmor.profile=unconfined"}, {}),
    "raw.idmap": ({"raw.idmap": "both 1000 1000"}, {}),
    "raw.qemu": ({"raw.qemu": "-device virtio-rng-pci"}, {}),
    "privileged": ({"security.privileged": "true"}, {}),
    "nesting": ({"security.nesting": "true"}, {}),
    "unix-char": ({}, {"nvidia0": {"type": "unix-char", "source": "/dev/nvidia0", "path": "/dev/nvidia0"}}),
    "unix-block": ({}, {"sda": {"type": "unix-block", "source": "/dev/sda"}}),
    "usb": ({}, {"u": {"type": "usb", "vendorid": "0403"}}),
    "pci": ({}, {"p": {"type": "pci", "address": "0000:01:00.0"}}),
    "gpu": ({}, {"g": {"type": "gpu"}}),
    "host-path-disk": ({}, {"lib": {"type": "disk", "source": "/usr/lib/aarch64-linux-gnu/tegra",
                                    "path": "/usr/lib/aarch64-linux-gnu/tegra", "readonly": "true"}}),
    "no-pool-disk": ({}, {"c": {"type": "disk", "source": "ceph:rbd/vol1", "path": "/mnt"}}),
    "macvlan": ({}, {"eth1": {"type": "nic", "nictype": "macvlan", "parent": "eth0"}}),
    "ipvlan": ({}, {"eth1": {"type": "nic", "nictype": "ipvlan", "parent": "eth0"}}),
    "sriov": ({}, {"eth1": {"type": "nic", "nictype": "sriov", "parent": "eth0"}}),
    "routed": ({}, {"eth1": {"type": "nic", "nictype": "routed", "ipv4.address": "10.0.0.5"}}),
    "tpm": ({}, {"t": {"type": "tpm"}}),
}
# What /proxy asks for each (api_key_capabilities.config_reaches_inside).
PROXY_PRICE = {"raw.lxc": "instance.exec", "raw.idmap": "instance.exec", "raw.qemu": "instance.exec",
               "privileged": "instance.exec", "nesting": "instance.exec", "host-path-disk": "instance.files"}
RAW_TARGETS = [("POST", f"/1.0/{family}", {"name": "c1", "source": {"type": "none"}}) for family in FAMILIES] + [
    ("PATCH", "/1.0/instances/web-1", {}), ("POST", "/1.0/profiles", {"name": "p9"}), ("PUT", "/1.0/profiles/p1", {})]


@pytest.mark.parametrize("item", list(HOST_REACH))
@pytest.mark.parametrize("method,path,base", RAW_TARGETS, ids=[*FAMILIES, "patch", "profile-create", "profile-put"])
def test_decision3_a_star_key_sends_host_reaching_config_on_the_raw_proxy(door: Any, method: str, path: str, base: Any,
                                                                          item: str) -> None:
    config, devices = HOST_REACH[item]
    body = {**base, "config": config, "devices": devices}
    _passed(door.raw(NO_GUEST, method, path, body=body), [], method, body)


@pytest.mark.parametrize("item", list(HOST_REACH))
def test_decision3_proxy_keeps_devs_price_for_host_reaching_config(door: Any, item: str) -> None:
    config, devices = HOST_REACH[item]
    seen = door.proxy(NO_GUEST, "create_instance", {"name": "c1", "source": {"type": "none"}, "config": config,
                                                    "devices": devices})
    if item in PROXY_PRICE:
        assert seen.status == 403 and PROXY_PRICE[item] in seen.text and seen.cluster.untouched
        assert [reason for reason, _ in seen.refusals] == ["capability_not_granted"]
    else:
        assert seen.status == 200 and [verb for verb, *_ in seen.cluster.writes] == ["create"] and seen.refusals == []


# OrcaHub's own bodies are still sent.

GPU_NODES = ["/dev/nvidia0", "/dev/nvidiactl", "/dev/nvmap", "/dev/nvgpu/igpu0/ctrl", "/dev/dri/renderD128"]
GPU_LIBS = ["/usr/lib/aarch64-linux-gnu/tegra", "/usr/lib/aarch64-linux-gnu/nvidia", "/usr/sbin/nvidia-smi"]
GPU_DEVICES = {**{n.strip("/").replace("/", "-"): {"type": "unix-char", "source": n, "path": n} for n in GPU_NODES},
               **{"lib-" + p.strip("/").replace("/", "-"): {"type": "disk", "source": p, "path": p, "readonly": "true"}
                  for p in GPU_LIBS}}
GPU_CONFIG = {"environment.LD_LIBRARY_PATH": "/usr/lib/aarch64-linux-gnu/tegra:/usr/lib/aarch64-linux-gnu/nvidia"}
EDITABLE = {"architecture": "aarch64", "config": {"limits.cpu": "2", "limits.memory": "2GiB",
                                                  "volatile.cloud-init.instance-id": "5b1e0a4c"},
            "devices": {"root": {"type": "disk", "path": "/", "pool": "default", "size": "20GiB"}},
            "profiles": ["default"], "description": "Plan - Order 1", "ephemeral": False, "stateful": False}
ROOT_DISK = {"type": "disk", "path": "/", "pool": "default", "size": "20GiB"}
CREATE = {"name": "vm-7", "type": "container", "source": {"type": "image", "fingerprint": "0f3c9a1b2c3d"},
          "profiles": ["default"],
          "devices": {"root": ROOT_DISK, "eth0": {"name": "eth0", "type": "nic", "network": "lxdbr0"}},
          "config": {"limits.cpu": "2", "limits.memory": "2GiB", "security.secureboot": "false"}}
ORCAHUB_WRITES = [
    ("POST", "/1.0/instances", CREATE),
    ("POST", "/1.0/instances", {**CREATE, "config": {**CREATE["config"], **GPU_CONFIG}, "devices": {**CREATE["devices"],
                                                                                                   **GPU_DEVICES}}),
    ("POST", "/1.0/instances", {**CREATE, "type": "virtual-machine", "config": {"boot.mode": "uefi-nosecureboot"},
                                "source": {"type": "image", "mode": "pull", "alias": "ubuntu/24.04",
                                           "server": "https://images.lxd.canonical.com", "protocol": "simplestreams"}}),
    ("PUT", "/1.0/instances/vm-7", {**EDITABLE, "config": {**EDITABLE["config"], "limits.cpu": "4"}}),
    ("PUT", "/1.0/instances/vm-7", {**EDITABLE, "config": {**EDITABLE["config"], **GPU_CONFIG},
                                    "devices": {**EDITABLE["devices"], **GPU_DEVICES}}),
    ("PATCH", "/1.0/instances/vm-7", {"config": {"boot.mode": "uefi-nosecureboot"}}),
    ("PATCH", "/1.0/instances/vm-7", {"config": {"snapshots.schedule": "@daily", "snapshots.expiry": "7d"}}),
    ("PATCH", "/1.0/instances/vm-7", {"devices": {"data": {"type": "disk", "pool": "default", "source": "vol-1",
                                                           "path": "/data"}}}),
    ("PUT", "/1.0/instances/vm-7", {"restore": "snap-0"}),
    *[("PUT", "/1.0/instances/vm-7/state", {"action": action, "force": action != "start"})
      for action in ("start", "stop", "restart", "freeze", "unfreeze")],
    ("PATCH", "/1.0/instances/vm-7", {"config": {"security.secureboot": "false"}}),
    ("PATCH", "/1.0/storage-pools/default/volumes/custom/vol-1",
     {"config": {"snapshots.schedule": "@daily", "snapshots.expiry": "7d", "snapshots.pattern": "snap%d"}}),
    ("DELETE", "/1.0/storage-pools/default/volumes/custom/vol-1/snapshots/snap-0", None),
    ("DELETE", "/1.0/operations/5b1e0a4c-0000-4000-8000-000000000001", None),
    ("POST", "/1.0/instances/vm-7/snapshots",
     {"name": "snap-1", "stateful": False, "expires_at": "2026-10-01T00:00:00Z"}),
    ("DELETE", "/1.0/instances/vm-7/snapshots/snap-0", None),
    ("DELETE", "/1.0/instances/vm-7", None),
    ("POST", "/1.0/storage-pools/default/volumes/custom", {"name": "vol-1", "content_type": "filesystem",
                                                            "config": {"size": "10GiB"}}),
    ("PATCH", "/1.0/storage-pools/default/volumes/custom/vol-1", {"config": {"size": "20GiB"}}),
    ("PUT", "/1.0/storage-pools/default/volumes/custom/vol-1", {"restore": "snap-0"}),
    ("POST", "/1.0/storage-pools/default/volumes/custom/vol-1/snapshots", {"name": "snap-1"}),
    ("DELETE", "/1.0/storage-pools/default/volumes/custom/vol-1", None),
    ("POST", "/1.0/projects", {"name": "cust-buyer", "description": "", "config": {
        "features.images": "false", "features.profiles": "false", "features.storage.volumes": "false",
        "features.networks": "false"}}),
    ("PATCH", "/1.0/projects/cust-buyer", {"config": {"limits.cpu": "4", "limits.memory": "8GiB"}}),
    ("PATCH", "/1.0/projects/cust-buyer", {"description": "Buyer", "config": {"user.description": "Buyer"}}),
    ("DELETE", "/1.0/projects/cust-buyer", None),
]


@pytest.mark.parametrize("held", [{}, {UD: X}, {CUD: X}], ids=["plain", "dashboard-create", "dashboard-rebuild"])
@pytest.mark.parametrize("method,path,body", ORCAHUB_WRITES,
                         ids=[f"{i}-{m}" for i, (m, *_r) in enumerate(ORCAHUB_WRITES)])
def test_orcahubs_own_writes_are_still_sent(door: Any, method: str, path: str, body: Any, held: Dict[str, Any]) -> None:
    project = None if "/projects" in path else "cust-buyer"
    if method == "PUT" and body and "config" in body:
        body = {**body, "config": {**body["config"], **held}}
    cluster = _held({**EDITABLE["config"], **held}, "/instances/vm-7", "cust-buyer")
    seen = door.raw(ORCAHUB, method, path, body=body, project=project, cluster=cluster)
    assert seen.status == 200 and seen.refusals == [], (seen.status, seen.text)
    assert seen.cluster.writes == [(method, body)]


@pytest.mark.parametrize("path", ORCAHUB_READS)
def test_orcahubs_own_reads_are_still_sent(door: Any, path: str) -> None:
    seen = door.raw(ORCAHUB, "GET", path, project="cust-buyer")
    assert seen.status < 500 and seen.refusals == [] and [verb for verb, *_ in seen.cluster.session.calls] == ["GET"]


@pytest.mark.parametrize("method", ["GET", "DELETE"])
@pytest.mark.parametrize("path", ["/1.0/instances/web-1", "/1.0/profiles/p1"])
def test_a_body_on_a_read_or_delete_is_never_read_for(door: Any, method: str, path: str) -> None:
    seen = door.raw(NO_GUEST, method, path, body={"config": {UD: X}})
    assert seen.refusals == [] and [verb for verb, *_ in seen.cluster.session.calls] == [method]
