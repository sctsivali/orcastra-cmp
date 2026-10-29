"""The system warnings feed carries the clusters' own LXD warnings.

`_fetch_warnings_for_node` returns a NodeRead. `/warnings/system/all` kept results only when
they were lists, so every LXD warning was dropped from the feed and its critical/warning/info
totals counted certificates and offline clusters alone. These pin the feed to the same
handling as `/warnings`: rows kept, a failed cluster named, the project bound and tenant ACL
applied.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core import org_scope
from app.routers import lxd_warnings
from app.routers.lxd_warnings import WarningInfo, get_all_system_warnings
from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.fanout import NodeRead


def _row(uuid: str, node: str, project: str = "default", severity: str = "high") -> WarningInfo:
    return WarningInfo(uuid=uuid, location="m1", source_node=node, project=project,
                       type="t", severity=severity, status="new", last_message="m")


class _Service:
    def __init__(self, nodes):
        self.connection_manager = SimpleNamespace(get_node_names=lambda: list(nodes))


@pytest.fixture(autouse=True)
def _no_vault(monkeypatch):
    import app.core.config as config

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(vault_enabled=False, vault_token=None))


@pytest.fixture(autouse=True)
def _no_kernel_rows(monkeypatch):
    from app.models.kernel_advisory_schemas import KernelAdvisoryResponse

    async def empty(*_a, **_k):
        return KernelAdvisoryResponse()

    monkeypatch.setattr(lxd_warnings, "build_kernel_report", empty)


def _serve(monkeypatch, by_node):
    def fetch(_service, node):
        result = by_node[node]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(lxd_warnings, "_fetch_warnings_for_node", fetch)


def _call(nodes):
    return asyncio.run(get_all_system_warnings(lxd_service=_Service(nodes)))


def test_lxd_warnings_reach_the_feed_and_its_totals(monkeypatch):
    _serve(monkeypatch, {
        "c1": NodeRead(rows=[_row("a", "c1", severity="high"), _row("b", "c1", severity="low")]),
        "c2": NodeRead(rows=[_row("c", "c2", severity="moderate")]),
    })
    body = _call(["c1", "c2"])
    assert sorted(w["uuid"] for w in body["lxd_warnings"]) == ["a", "b", "c"]
    assert body["total"] == 3
    assert (body["critical"], body["info"]) == (1, 1)
    assert body["unreachable_nodes"] == [] and body["partial_nodes"] == []


def test_a_cluster_that_failed_is_named_not_counted_as_empty(monkeypatch):
    _serve(monkeypatch, {
        "c1": NodeRead(rows=[_row("a", "c1")]),
        "dead": LXDConnectionError("Could not connect to dead"),
    })
    body = _call(["c1", "dead"])
    assert [w["uuid"] for w in body["lxd_warnings"]] == ["a"]
    assert body["unreachable_nodes"] == ["dead"]
    assert body["node_errors"]["dead"].startswith("unreachable")


def test_a_partial_read_keeps_its_rows_and_is_marked_partial(monkeypatch):
    read = NodeRead(rows=[_row("a", "c1")])
    read.lost("warning x: ValueError")
    _serve(monkeypatch, {"c1": read})
    body = _call(["c1"])
    assert [w["uuid"] for w in body["lxd_warnings"]] == ["a"]
    assert body["partial_nodes"] == ["c1"]


def test_the_project_bound_still_holds(monkeypatch):
    _serve(monkeypatch, {"c1": NodeRead(rows=[_row("mine", "c1", "ours"), _row("theirs", "c1", "other")])})
    token = org_scope._visible_projects_var.set({"c1": {"ours"}})
    try:
        body = _call(["c1"])
    finally:
        org_scope._visible_projects_var.reset(token)
    assert [w["uuid"] for w in body["lxd_warnings"]] == ["mine"]


def test_a_tenant_acl_drops_unpermitted_projects_and_hides_reasons(monkeypatch):
    _serve(monkeypatch, {
        "c1": NodeRead(rows=[_row("ok", "c1", "granted"), _row("no", "c1", "other")]),
        "dead": LXDConnectionError("Could not connect to dead"),
    })
    token = org_scope._tenant_acl_var.set({"c1": {("granted", None): {"view"}}})
    try:
        body = _call(["c1", "dead"])
    finally:
        org_scope._tenant_acl_var.reset(token)
    assert [w["uuid"] for w in body["lxd_warnings"]] == ["ok"]
    assert body["unreachable_nodes"] == ["dead"]
    assert body["node_errors"] == {}


def _kernel_report(state: str, severity: str):
    from datetime import date

    from app.models.kernel_advisory_schemas import (
        HostKernelRow,
        KernelAdvisoryFinding,
        KernelAdvisoryInfo,
        KernelAdvisoryResponse,
    )

    from datetime import datetime, timezone

    checked = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)
    finding = KernelAdvisoryFinding(advisory_id="CVE-2026-99999", state=state, severity=severity,
                                    reason="r", check_command="uname -r", reference_url="https://x",
                                    fixed_in="5.15.0-203.213", source="ubuntu", checked_at=checked)
    info = KernelAdvisoryInfo(id="CVE-2026-99999", summary="s", reference_url="https://x",
                              data_as_of=date(2026, 10, 19), introduced="2.6.27", fixed_mainline="7.3-rc7",
                              source="ubuntu", checked_at=checked, ubuntu_priority="medium",
                              ubuntu_url="https://ubuntu.com/security/CVE-2026-99999")
    host = HostKernelRow(cluster_id="c1", display_name="C1", member="m1",
                         kernel_version="5.15.0-185-generic", findings=[finding])
    return KernelAdvisoryResponse(advisories=[info], hosts=[host])


def test_kernel_advisories_join_the_feed_and_its_totals(monkeypatch):
    _serve(monkeypatch, {"c1": NodeRead(rows=[_row("a", "c1", severity="low")])})

    async def report(*_a, **_k):
        return _kernel_report("exposed", "critical")

    monkeypatch.setattr(lxd_warnings, "build_kernel_report", report)
    body = _call(["c1"])
    assert [(k["member"], k["warning_type"], k["severity"]) for k in body["kernel_advisories"]] == [
        ("m1", "kernel_exposed", "critical")
    ]
    row = body["kernel_advisories"][0]
    assert row["data_as_of"] == "2026-10-19"
    # where the verdict came from and when, so the tab can say how current it is
    assert (row["source"], row["checked_at"], row["fixed_in"]) == ("ubuntu", "2026-10-29T10:00:00Z", "5.15.0-203.213")
    assert (row["ubuntu_priority"], row["ubuntu_url"]) == ("medium", "https://ubuntu.com/security/CVE-2026-99999")
    assert (body["total"], body["critical"], body["info"]) == (2, 1, 1)


def test_a_patched_host_adds_no_warning(monkeypatch):
    _serve(monkeypatch, {"c1": NodeRead()})

    async def report(*_a, **_k):
        return _kernel_report("patched", "info")

    monkeypatch.setattr(lxd_warnings, "build_kernel_report", report)
    body = _call(["c1"])
    assert body["kernel_advisories"] == [] and body["total"] == 0


def test_a_failed_kernel_read_leaves_the_rest_of_the_feed(monkeypatch):
    _serve(monkeypatch, {"c1": NodeRead(rows=[_row("a", "c1")])})

    async def broken(*_a, **_k):
        raise RuntimeError("vault unreachable")

    monkeypatch.setattr(lxd_warnings, "build_kernel_report", broken)
    body = _call(["c1"])
    assert [w["uuid"] for w in body["lxd_warnings"]] == ["a"]
    assert body["kernel_advisories"] == []
