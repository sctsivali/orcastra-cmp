"""A purchase inside the holding party is checked for host reach in what the buyer can reach.

A purchase on a cluster its seller's party holds skips the whole-cluster share audit, which every
stock cluster fails. What the buyer reaches still has to be safe: an OrcaHub project leaves
features.profiles off, so its instances inherit the `default` project's default profile, and a
tenant with console is root inside them. The purchased project's instances (local and expanded) and
the default profile they inherit are checked for host reach (privileged, raw.*, host disks,
passthrough and proxy devices); a stock profile with a pool root disk and a managed NIC passes. The
report stays value free.
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.services import purchase_reach_audit as reach
from app.services.shared_cluster_audit import SharedClusterGuardError

STOCK_PROFILE = {
    "name": "default",
    "config": {},
    "devices": {
        "root": {"type": "disk", "path": "/", "pool": "default"},
        "eth0": {"type": "nic", "network": "lxdbr0", "name": "eth0"},
    },
}
PLAIN_INSTANCE = {
    "name": "buyer-vm",
    "config": {"limits.cpu": "1", "user.orca.credential.password": "SECRET_CREDENTIAL"},
    "devices": {},
    "expanded_config": {"limits.cpu": "1", "user.orca.credential.password": "SECRET_CREDENTIAL"},
    "expanded_devices": STOCK_PROFILE["devices"],
}
OH_PROJECT = {"name": "buyer", "config": {"features.profiles": "false", "features.images": "false"}}


class _Response:
    def __init__(self, rows: List[Any]) -> None:
        self._rows = rows

    def json(self) -> Dict[str, Any]:
        return {"metadata": self._rows}


class _Collection:
    def __init__(self, by_project: Dict[Optional[str], List[Any]]) -> None:
        self.by_project = by_project
        self.asked: List[Optional[str]] = []

    def get(self, params: Optional[Dict[str, Any]] = None, timeout: Any = None) -> _Response:
        project = (params or {}).get("project")
        self.asked.append(project)
        return _Response(self.by_project.get(project, []))


def _service(projects: List[Any], profiles: Dict[Optional[str], List[Any]], instances: Dict[Optional[str], List[Any]]):
    api = SimpleNamespace(
        projects=_Collection({None: projects}),
        profiles=_Collection(profiles),
        instances=_Collection(instances),
    )
    client = SimpleNamespace(api=api)
    return SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda _cluster: client)), api


def _scan(service: Any, project: str = "buyer"):
    return asyncio.run(reach.audit_purchased_project("c1", project, lxd_service=service))


def test_a_stock_cluster_sells_a_plain_purchase() -> None:
    service, api = _service([{"name": "default", "config": {}}, OH_PROJECT], {"default": [STOCK_PROFILE]}, {"buyer": [PLAIN_INSTANCE]})
    report = _scan(service)
    assert (report.complete, report.safe) == (True, True)
    assert api.profiles.asked == ["default"], "features.profiles off: the default project's profile is the one in force"
    assert api.instances.asked == ["buyer"]
    assert (report.projects_checked, report.profiles_checked, report.instances_checked) == (1, 1, 1)


@pytest.mark.parametrize(
    "profile_change",
    [
        {"config": {"security.privileged": "true"}},
        {"config": {"raw.lxc": "lxc.apparmor.profile=unconfined"}},
        {"devices": {**STOCK_PROFILE["devices"], "hostdir": {"type": "disk", "source": "/srv", "path": "/mnt"}}},
        {"devices": {**STOCK_PROFILE["devices"], "gpu0": {"type": "pci", "address": "0000:01:00.0"}}},
    ],
)
def test_an_inherited_default_profile_that_reaches_the_host_refuses(profile_change: Dict[str, Any]) -> None:
    profile = {**STOCK_PROFILE, **profile_change}
    service, _api = _service([OH_PROJECT], {"default": [profile]}, {"buyer": [PLAIN_INSTANCE]})
    report = _scan(service)
    assert (report.complete, report.safe) == (True, False)
    assert {f.scope for f in report.findings} == {"profile"}


def test_an_instance_that_reaches_the_host_refuses() -> None:
    privileged = {**PLAIN_INSTANCE, "expanded_config": {"security.privileged": "true"}}
    service, _api = _service([OH_PROJECT], {"default": [STOCK_PROFILE]}, {"buyer": [privileged]})
    report = _scan(service)
    assert report.safe is False
    assert {(f.scope, f.code, f.field) for f in report.findings} == {("instance_effective", "host_reach_not_allowed", "config")}


def test_a_project_with_its_own_profiles_is_judged_by_its_own() -> None:
    own = {"name": "buyer", "config": {"features.profiles": "true"}}
    risky = {**STOCK_PROFILE, "config": {"security.privileged": "true"}}
    service, api = _service([own], {"default": [risky], "buyer": [STOCK_PROFILE]}, {"buyer": [PLAIN_INSTANCE]})
    report = _scan(service)
    assert report.safe is True
    assert api.profiles.asked == ["buyer"]


def test_the_sellers_other_profiles_are_not_the_buyers() -> None:
    gpu = {"name": "gpu", "config": {}, "devices": {"gpu0": {"type": "pci", "address": "0000:01:00.0"}}}
    service, _api = _service([OH_PROJECT], {"default": [STOCK_PROFILE, gpu]}, {"buyer": [PLAIN_INSTANCE]})
    assert _scan(service).safe is True


def test_a_project_not_created_yet_checks_the_profile_it_will_inherit() -> None:
    risky = {**STOCK_PROFILE, "config": {"security.privileged": "true"}}
    service, api = _service([{"name": "default", "config": {}}], {"default": [risky]}, {})
    report = _scan(service)
    assert report.safe is False
    assert api.instances.asked == [], "nothing to read in a project that does not exist"


def test_the_report_carries_no_value_or_name() -> None:
    profile = {**STOCK_PROFILE, "config": {"raw.lxc": "SECRET_RAW"}}
    instance = {**PLAIN_INSTANCE, "expanded_devices": {"dev": {"type": "unix-char", "source": "/dev/SECRET_DEV"}}}
    service, _api = _service([OH_PROJECT], {"default": [profile]}, {"buyer": [instance]})
    encoded = json.dumps(_scan(service).as_dict())
    for forbidden in ("SECRET_RAW", "SECRET_DEV", "SECRET_CREDENTIAL", "buyer-vm", "unix-char", "raw.lxc"):
        assert forbidden not in encoded


def test_an_unreachable_cluster_is_incomplete() -> None:
    service = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda _cluster: None))
    report = _scan(service)
    assert (report.complete, report.safe) == (False, False)


def test_a_malformed_inventory_is_incomplete() -> None:
    service, api = _service([OH_PROJECT], {"default": [STOCK_PROFILE]}, {"buyer": ["/1.0/instances/x"]})
    assert _scan(service).complete is False


def _refusal(report: Any) -> SharedClusterGuardError:
    async def fixed(_cluster: str, _project: str, **_kwargs: Any) -> Any:
        return report

    original = reach.audit_purchased_project
    reach.audit_purchased_project = fixed
    try:
        with pytest.raises(SharedClusterGuardError) as refused:
            asyncio.run(reach.assert_purchase_reach_safe("c1", "buyer"))
        return refused.value
    finally:
        reach.audit_purchased_project = original


def test_an_unsafe_purchase_answers_the_codes_orcahub_reads() -> None:
    service, _api = _service([OH_PROJECT], {"default": [{**STOCK_PROFILE, "config": {"security.privileged": "1"}}]}, {})
    unsafe = _refusal(_scan(service))
    assert (unsafe.status_code, unsafe.code) == (409, "cluster_not_safe_to_share")
    missing = _refusal(_scan(SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda _c: None))))
    assert (missing.status_code, missing.code) == (503, "cluster_share_audit_unavailable")


def test_a_safe_purchase_passes() -> None:
    service, _api = _service([OH_PROJECT], {"default": [STOCK_PROFILE]}, {"buyer": [PLAIN_INSTANCE]})
    report = _scan(service)

    async def fixed(_cluster: str, _project: str, **_kwargs: Any) -> Any:
        return report

    original = reach.audit_purchased_project
    reach.audit_purchased_project = fixed
    try:
        assert asyncio.run(reach.assert_purchase_reach_safe("c1", "buyer")) is report
    finally:
        reach.audit_purchased_project = original


@pytest.mark.parametrize("nictype", ["macvlan", "ipvlan", "sriov", "routed", "physical"])
def test_a_nic_on_a_host_interface_refuses(nictype: str) -> None:
    profile = {**STOCK_PROFILE, "devices": {**STOCK_PROFILE["devices"], "eth0": {"type": "nic", "nictype": nictype, "parent": "eno1"}}}
    service, _api = _service([OH_PROJECT], {"default": [profile]}, {"buyer": [PLAIN_INSTANCE]})
    report = _scan(service)
    assert report.safe is False
    assert {(f.scope, f.field) for f in report.findings} == {("profile", "devices")}


def test_an_old_style_bridged_nic_on_lxdbr0_still_sells() -> None:
    profile = {**STOCK_PROFILE, "devices": {**STOCK_PROFILE["devices"], "eth0": {"type": "nic", "nictype": "bridged", "parent": "lxdbr0"}}}
    service, _api = _service([OH_PROJECT], {"default": [profile]}, {"buyer": [PLAIN_INSTANCE]})
    assert _scan(service).safe is True
