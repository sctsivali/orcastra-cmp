"""An instance create must be refused at the boundary with a reason, not left spinning.

LXD accepts a create whose network the target project cannot reach as an async operation and
fails it in the background. The console's placeholder row has no path to learn that, so the
instance would sit at "Creating..." with empty IP, Profiles and Root Disk columns.

Two cases are pinned first: the NIC normally arrives from a profile rather than from the
request, and it can name its network with `parent` instead of `network`.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.schemas import InstanceCreateRequest
from app.services.lxd.create_preflight import (
    CreatePreflightEvaluation,
    _inspect_create,
)
from app.services.lxd.nic_preflight import (
    check_address_for_network,
    NicPreflightError,
    check_nics,
    collect_nics,
    merge_effective_devices,
)

DEFAULT_PROJECT = "default"


class FakeNode:
    def __init__(self, client, collection, item=None):
        self._client = client
        self._collection = collection
        self._item = item

    def __getitem__(self, item):
        return FakeNode(self._client, self._collection, str(item))

    def get(self, params=None, timeout=None):
        params = params or {}
        project = params.get("project", DEFAULT_PROJECT)
        if self._collection == "projects":
            if self._item is not None:
                payload = {
                    "name": self._item,
                    "config": self._client.projects[self._item],
                }
            else:
                payload = [{"name": n, "config": c} for n, c in self._client.projects.items()]
        elif self._collection == "networks":
            payload = self._client.networks.get(project, [])
        elif self._collection == "profiles":
            payload = self._client.profiles.get(project, [])
        elif self._collection == "images":
            payload = self._client.images.get(project, [])
        elif self._collection == "storage-pools" and self._item is not None:
            payload = self._client.pools[self._item]
        else:
            payload = self._client.instances.get(project, [])
        self._client.reads.append((self._collection, project))
        return FakeResponse(payload)


class FakeResponse:
    def __init__(self, metadata):
        self._metadata = metadata

    def json(self):
        return {"metadata": self._metadata}


class FakeApi:
    def __init__(self, client):
        self._client = client

    def __getitem__(self, collection):
        return FakeNode(self._client, collection)

    @property
    def projects(self):
        return FakeNode(self._client, "projects")

    @property
    def storage_pools(self):
        return FakeNode(self._client, "storage-pools")


class FakeClient:
    def __init__(
        self,
        projects=None,
        networks=None,
        profiles=None,
        instances=None,
        images=None,
        pools=None,
    ):
        self.projects = projects or {DEFAULT_PROJECT: {}}
        self.networks = networks or {}
        self.profiles = profiles or {}
        self.instances = instances or {}
        self.images = images or {
            DEFAULT_PROJECT: [
                {
                    "fingerprint": "a" * 64,
                    "aliases": [{"name": "ubuntu/24.04"}],
                    "type": "virtual-machine",
                }
            ]
        }
        self.pools = pools or {"local": {"name": "local", "driver": "dir", "status": "Created"}}
        self.reads = []
        self.project = None
        self.api = FakeApi(self)


def bridge(name, subnet="10.0.5.1/24", managed=True):
    return {"name": name, "managed": managed, "config": {"ipv4.address": subnet}}


ISOLATED = {
    DEFAULT_PROJECT: {"features.networks": "true", "features.profiles": "true"},
    "UAT-Herald": {"features.networks": "true", "features.profiles": "true"},
}


def client_with_isolated_project():
    return FakeClient(
        projects=ISOLATED,
        networks={
            DEFAULT_PROJECT: [bridge("br-uplink"), bridge("lxdbr0")],
            "UAT-Herald": [bridge("ovn-uat-herald", "192.168.3.1/24")],
        },
        profiles={
            DEFAULT_PROJECT: [{"name": "default", "devices": {}}],
            "UAT-Herald": [{"name": "default", "devices": {}}],
        },
    )


class TestTheCasesTheAuditFiled:
    def test_a_network_from_another_project_is_refused(self):
        """Create in UAT-Herald with br-uplink, a network only the `default` project has."""
        client = client_with_isolated_project()
        with pytest.raises(NicPreflightError) as exc:
            check_nics(
                client,
                "UAT-Herald",
                {"eth0": {"type": "nic", "network": "br-uplink"}},
                ["default"],
            )
        assert "br-uplink" in str(exc.value)
        assert "does not exist in project 'UAT-Herald'" in str(exc.value)

    def test_the_refusal_names_what_the_project_can_actually_use(self):
        client = client_with_isolated_project()
        with pytest.raises(NicPreflightError) as exc:
            check_nics(client, "UAT-Herald", {"eth0": {"type": "nic", "network": "lxdbr0"}}, ["default"])
        assert "ovn-uat-herald" in str(exc.value)

    def test_the_project_s_own_network_is_accepted(self):
        client = client_with_isolated_project()
        check_nics(
            client,
            "UAT-Herald",
            {"eth0": {"type": "nic", "network": "ovn-uat-herald"}},
            ["default"],
        )

    def test_a_nic_inherited_from_a_profile_is_checked_too(self):
        """The console submits profiles: ["default"] and lets the profile supply eth0, so a
        check that only reads the request passes exactly the case that hangs."""
        client = FakeClient(
            projects=ISOLATED,
            networks={
                DEFAULT_PROJECT: [bridge("br-uplink")],
                "UAT-Herald": [bridge("ovn-uat-herald", "192.168.3.1/24")],
            },
            profiles={
                "UAT-Herald": [
                    {
                        "name": "default",
                        "devices": {"eth0": {"type": "nic", "network": "br-uplink"}},
                    }
                ]
            },
        )
        with pytest.raises(NicPreflightError) as exc:
            check_nics(client, "UAT-Herald", None, ["default"])
        assert "br-uplink" in str(exc.value)

    def test_a_nic_naming_its_network_with_parent_is_checked_too(self):
        """A bare `parent`, with no nictype, is still read as a network reference."""
        client = client_with_isolated_project()
        with pytest.raises(NicPreflightError):
            check_nics(
                client,
                "UAT-Herald",
                {"eth0": {"type": "nic", "parent": "br-uplink"}},
                ["default"],
            )

    @pytest.mark.parametrize("nictype", ["macvlan", "physical", "sriov", "ipvlan", "routed", "bridged"])
    def test_a_parent_that_names_a_host_interface_is_not_a_missing_network(self, nictype):
        """A stock LXD host's default profile hands out `eth0` as macvlan on a physical parent.

        `enp86s0` is listed by /1.0/networks with managed:false, so reading `parent` as a
        network reference would resolve it and then fail the managed test, refusing a create
        the daemon would take. LXD resolves a host parent itself, so there is nothing here to
        refuse.
        """
        client = client_with_isolated_project()
        check_nics(
            client,
            "UAT-Herald",
            {"eth0": {"type": "nic", "nictype": nictype, "parent": "enp86s0"}},
            ["default"],
        )

    def test_an_inherited_macvlan_nic_does_not_block_a_create(self):
        """A default profile with a macvlan eth0 on a host interface and a root disk."""
        client = FakeClient(
            projects={DEFAULT_PROJECT: {}},
            networks={DEFAULT_PROJECT: [bridge("lxdbr0", "10.0.5.1/24")]},
            profiles={
                DEFAULT_PROJECT: [
                    {
                        "name": "default",
                        "devices": {
                            "eth0": {"type": "nic", "nictype": "macvlan", "parent": "enp86s0"},
                            "root": {"type": "disk", "path": "/", "pool": "hdd"},
                        },
                    }
                ]
            },
        )
        check_nics(client, None, {}, ["default"])

    def test_an_explicit_network_is_still_refused_when_the_project_lacks_it(self):
        """`network=` is a managed reference, so a project that lacks it is refused."""
        client = client_with_isolated_project()
        with pytest.raises(NicPreflightError):
            check_nics(
                client,
                "UAT-Herald",
                {"eth0": {"type": "nic", "network": "lxdbr0"}},
                ["default"],
            )


class TestMergingRequestAndProfileDevices:
    def test_a_request_device_overrides_the_profile_device_of_the_same_name(self):
        nics = collect_nics(
            {"eth0": {"type": "nic", "network": "chosen"}},
            [{"eth0": {"type": "nic", "network": "inherited"}}],
        )
        assert nics == [("eth0", {"type": "nic", "network": "chosen"})]

    def test_a_differently_named_request_device_adds_an_interface(self):
        nics = collect_nics(
            {"eth1": {"type": "nic", "network": "extra"}},
            [{"eth0": {"type": "nic", "network": "inherited"}}],
        )
        assert [n for n, _ in nics] == ["eth0", "eth1"]

    def test_devices_that_are_not_nics_are_ignored(self):
        nics = collect_nics({"root": {"type": "disk", "pool": "local"}}, [])
        assert nics == []

    def test_type_none_removes_an_inherited_nic(self):
        nics = collect_nics(
            {"eth0": {"type": "none"}},
            [{"eth0": {"type": "nic", "network": "inherited"}}],
        )
        assert nics == []

    def test_profiles_are_merged_in_the_requested_order_before_instance_overrides(self):
        effective = merge_effective_devices(
            {"data": {"type": "disk", "pool": "instance-pool"}},
            [
                {
                    "eth0": {"type": "nic", "network": "first"},
                    "data": {"type": "disk", "pool": "first-pool"},
                },
                {"eth0": {"type": "nic", "network": "second"}},
            ],
        )
        assert effective["eth0"]["network"] == "second"
        assert effective["data"]["pool"] == "instance-pool"


class TestInheritedProjects:
    def test_a_project_without_its_own_networks_is_checked_against_default(self):
        client = FakeClient(
            projects={
                DEFAULT_PROJECT: {"features.networks": "true"},
                "shared": {"features.networks": "false", "features.profiles": "true"},
            },
            networks={DEFAULT_PROJECT: [bridge("lxdbr0")]},
            profiles={"shared": [{"name": "default", "devices": {}}]},
        )
        check_nics(client, "shared", {"eth0": {"type": "nic", "network": "lxdbr0"}}, ["default"])
        assert ("networks", DEFAULT_PROJECT) in client.reads


class TestFixedAddresses:
    """A NIC may pin its IPv4, and a bad one must not reach the daemon."""

    def _client(self):
        return FakeClient(
            projects={DEFAULT_PROJECT: {}},
            networks={DEFAULT_PROJECT: [bridge("lxdbr0", "10.0.5.1/24")]},
            profiles={DEFAULT_PROJECT: [{"name": "default", "devices": {}}]},
        )

    def _nic(self, address):
        return {"eth0": {"type": "nic", "network": "lxdbr0", "ipv4.address": address}}

    def test_an_address_inside_the_subnet_is_accepted(self):
        check_nics(self._client(), None, self._nic("10.0.5.42"), ["default"])

    def test_an_address_outside_the_subnet_is_refused(self):
        with pytest.raises(NicPreflightError) as exc:
            check_nics(self._client(), None, self._nic("192.168.9.9"), ["default"])
        assert "outside" in str(exc.value) and "10.0.5.0/24" in str(exc.value)

    @pytest.mark.parametrize("bad", ["not-an-ip", "10.0.5.999", "", "10.0.5"])
    def test_a_malformed_address_is_refused(self, bad):
        nic = {"eth0": {"type": "nic", "network": "lxdbr0", "ipv4.address": bad}}
        if bad == "":
            check_nics(self._client(), None, nic, ["default"])
            return
        with pytest.raises(NicPreflightError) as exc:
            check_nics(self._client(), None, nic, ["default"])
        assert "invalid IPv4" in str(exc.value)

    @pytest.mark.parametrize("edge", ["10.0.5.0", "10.0.5.255"])
    def test_the_network_and_broadcast_addresses_are_refused(self, edge):
        with pytest.raises(NicPreflightError) as exc:
            check_nics(self._client(), None, self._nic(edge), ["default"])
        assert "address of" in str(exc.value)

    def test_a_fixed_address_on_a_network_with_no_subnet_is_refused(self):
        client = FakeClient(
            projects={DEFAULT_PROJECT: {}},
            networks={DEFAULT_PROJECT: [{"name": "ovn0", "managed": True, "config": {}}]},
            profiles={DEFAULT_PROJECT: [{"name": "default", "devices": {}}]},
        )
        nic = {"eth0": {"type": "nic", "network": "ovn0", "ipv4.address": "10.0.5.42"}}
        with pytest.raises(NicPreflightError) as exc:
            check_nics(client, None, nic, ["default"])
        assert "no IPv4 subnet" in str(exc.value)


class TestNotInventingRefusals:
    def test_an_unmanaged_network_is_refused_with_its_own_reason(self):
        client = FakeClient(
            projects={DEFAULT_PROJECT: {}},
            networks={DEFAULT_PROJECT: [bridge("docker0", managed=False)]},
            profiles={DEFAULT_PROJECT: [{"name": "default", "devices": {}}]},
        )
        with pytest.raises(NicPreflightError) as exc:
            check_nics(client, None, {"eth0": {"type": "nic", "network": "docker0"}}, ["default"])
        assert "does not manage" in str(exc.value)

    def test_the_single_address_check_refuses_an_unmanaged_network_the_same_way(self):
        """The two entry points must agree, or the same mistake reads differently.

        LXD lists a bare host interface as an unmanaged network, so a NIC whose `parent` is one
        resolves and then fails whatever test comes next. Judged on its subnet it reports having
        none, which is true and sends the operator looking for a subnet to add.
        """
        client = FakeClient(
            projects={DEFAULT_PROJECT: {}},
            networks={DEFAULT_PROJECT: [{"name": "enp86s0", "managed": False, "config": {}}]},
            profiles={DEFAULT_PROJECT: [{"name": "default", "devices": {}}]},
        )
        with pytest.raises(NicPreflightError) as exc:
            check_address_for_network(client, None, "enp86s0", "10.0.5.42")
        assert "does not manage" in str(exc.value)
        assert "no IPv4 subnet" not in str(exc.value)

    def test_a_create_with_no_nic_at_all_is_left_alone(self):
        check_nics(client_with_isolated_project(), "UAT-Herald", {}, ["default"])

    def test_a_failed_network_read_does_not_become_a_refusal(self):
        """An unreadable cluster is not a bad NIC. Let LXD answer rather than inventing a
        rejection the operator cannot act on."""

        class Exploding(FakeClient):
            def __init__(self):
                super().__init__(projects={DEFAULT_PROJECT: {}})

            @property
            def api(self):
                raise RuntimeError("cluster unreachable")

            @api.setter
            def api(self, _value):
                pass

        check_nics(Exploding(), None, {"eth0": {"type": "nic", "network": "x"}}, ["default"])


class TestTheRefusalReachesTheCallerAsAClientError:
    """The create route maps LXDOperationError to a status by substring.

    The console renders a 500 as an outage and a 503 as "try again later", and neither tells the
    operator to pick a different network, so the wording of these refusals is load-bearing.
    """

    # Mirrors routers/instances.py create_instance, which is where the mapping lives.
    @staticmethod
    def _status_for(message: str) -> int:
        low = message.lower()
        if "already exists" in low:
            return 409
        if "not found" in low:
            return 404
        if "connect" in low or "not available" in low or "could not open project" in low:
            return 503
        return 400

    def _messages(self):
        client = client_with_isolated_project()
        empty = FakeClient(
            projects=ISOLATED,
            networks={DEFAULT_PROJECT: [bridge("br-uplink")], "UAT-Herald": []},
            profiles={"UAT-Herald": [{"name": "default", "devices": {}}]},
        )
        subnet_client = FakeClient(
            projects={DEFAULT_PROJECT: {}},
            networks={DEFAULT_PROJECT: [bridge("lxdbr0", "10.0.5.1/24")]},
            profiles={DEFAULT_PROJECT: [{"name": "default", "devices": {}}]},
        )
        cases = [
            (client, "UAT-Herald", {"eth0": {"type": "nic", "network": "br-uplink"}}),
            (empty, "UAT-Herald", {"eth0": {"type": "nic", "network": "br-uplink"}}),
            (subnet_client, None, {"eth0": {"type": "nic", "network": "lxdbr0", "ipv4.address": "9.9.9.9"}}),
            (subnet_client, None, {"eth0": {"type": "nic", "network": "lxdbr0", "ipv4.address": "nope"}}),
            (subnet_client, None, {"eth0": {"type": "nic", "network": "lxdbr0", "ipv4.address": "10.0.5.0"}}),
        ]
        for c, project, devices in cases:
            with pytest.raises(NicPreflightError) as exc:
                check_nics(c, project, devices, ["default"])
            yield str(exc.value)

    def test_every_refusal_is_a_400(self):
        for message in self._messages():
            assert self._status_for(message) == 400, message


class _PreflightService:
    def __init__(self, client):
        self.client = client

    def get_client_with_reconnect(self, _host):
        return self.client


def _preflight_client(profile_devices, *, instances=None, profile_rows=None):
    return FakeClient(
        projects={DEFAULT_PROJECT: {}},
        networks={DEFAULT_PROJECT: [bridge("lxdbr0", "10.0.5.1/24")]},
        profiles={DEFAULT_PROJECT: profile_rows or [{"name": "default", "devices": profile_devices}]},
        instances={DEFAULT_PROJECT: instances or []},
    )


def _request(**changes):
    body = {
        "name": "web01",
        "host": "nuc1",
        "image": "ubuntu/24.04",
        "project": DEFAULT_PROJECT,
    }
    body.update(changes)
    return InstanceCreateRequest(**body)


class TestCreatePreflight:
    def run(self, client, **request_changes):
        return _inspect_create(_PreflightService(client), _request(**request_changes)).response

    def test_an_explicit_nic_is_reported_as_effective(self):
        client = _preflight_client({"root": {"type": "disk", "path": "/", "pool": "local"}})
        result = self.run(
            client,
            devices={"eth0": {"type": "nic", "network": "lxdbr0"}},
        )
        assert result.ready is True
        assert result.effective.nic_names == ["eth0"]
        assert result.required_acknowledgements == []

    def test_an_inherited_nic_is_reported_as_effective(self):
        client = _preflight_client(
            {
                "root": {"type": "disk", "path": "/", "pool": "local"},
                "eth0": {"type": "nic", "network": "lxdbr0"},
            }
        )
        result = self.run(client)
        assert result.ready is True
        assert result.effective.nic_names == ["eth0"]

    def test_type_none_removes_the_inherited_nic_and_requires_acknowledgement(self):
        client = _preflight_client(
            {
                "root": {"type": "disk", "path": "/", "pool": "local"},
                "eth0": {"type": "nic", "network": "lxdbr0"},
            }
        )
        result = self.run(client, devices={"eth0": {"type": "none"}})
        assert result.ready is False
        assert result.effective.nic_names == []
        assert result.warnings[0].code == "NETWORKLESS"
        assert result.required_acknowledgements == ["networkless"]

        accepted = self.run(
            client,
            devices={"eth0": {"type": "none"}},
            acknowledgements=["networkless"],
        )
        assert accepted.ready is True
        # Acknowledging a warning must not change the plan it acknowledges.
        assert accepted.plan_revision == result.plan_revision

    def test_profile_order_is_request_order_not_daemon_list_order(self):
        profiles = [
            {
                "name": "second",
                "devices": {"eth0": {"type": "nic", "network": "lxdbr0"}},
            },
            {
                "name": "first",
                "devices": {
                    "root": {"type": "disk", "path": "/", "pool": "local"},
                    "eth0": {"type": "nic", "network": "missing"},
                },
            },
        ]
        client = _preflight_client({}, profile_rows=profiles)
        result = self.run(client, profiles=["first", "second"])
        assert result.ready is True
        assert not result.blockers

    def test_duplicate_and_invalid_network_are_structured_blockers(self):
        client = _preflight_client(
            {"root": {"type": "disk", "path": "/", "pool": "local"}},
            instances=[{"name": "web01"}],
        )
        result = self.run(
            client,
            devices={"eth0": {"type": "nic", "network": "missing"}},
        )
        assert result.ready is False
        assert {blocker.code for blocker in result.blockers} == {
            "INSTANCE_ALREADY_EXISTS",
            "NETWORK_INVALID",
        }

    def test_profile_state_changes_the_plan_revision(self):
        client = _preflight_client({"root": {"type": "disk", "path": "/", "pool": "local"}})
        before = self.run(client, acknowledgements=["networkless"])
        client.profiles[DEFAULT_PROJECT][0]["devices"]["eth0"] = {
            "type": "nic",
            "network": "lxdbr0",
        }
        after = self.run(client)
        assert before.plan_revision != after.plan_revision

    def test_effective_maps_are_available_only_to_server_side_policy(self):
        client = _preflight_client(
            {},
            profile_rows=[
                {
                    "name": "default",
                    "config": {"limits.cpu": "2"},
                    "devices": {
                        "root": {"type": "disk", "path": "/", "pool": "local"},
                        "eth0": {"type": "nic", "network": "lxdbr0"},
                    },
                }
            ],
        )
        evaluation = _inspect_create(
            _PreflightService(client),
            _request(config={"limits.cpu": "4", "security.nesting": "true"}),
        )
        assert evaluation.effective_config == {
            "limits.cpu": "4",
            "security.nesting": "true",
        }
        assert evaluation.effective_devices["eth0"]["network"] == "lxdbr0"
        public = evaluation.response.model_dump(mode="json")
        assert "effective_config" not in public
        assert "effective_devices" not in public

    def test_unknown_top_level_fields_are_refused(self):
        with pytest.raises(ValidationError):
            _request(unknown_create_option=True)


class TestCreateRoutePreflightGate:
    def test_a_stale_plan_never_reaches_the_create_call(self, monkeypatch):
        from app.routers import instances as instances_router
        from app.models.schemas import (
            InstanceCreateEffectiveSummary,
            InstanceCreatePreflightResponse,
        )

        async def fresh(_request, _service):
            return CreatePreflightEvaluation(
                response=InstanceCreatePreflightResponse(
                    ready=True,
                    plan_revision="b" * 64,
                    effective=InstanceCreateEffectiveSummary(
                        profiles=["default"], nic_names=["eth0"], root_device="root"
                    ),
                ),
                effective_config={},
                effective_devices={},
            )

        class NeverCreate:
            async def create_instance(self, **_kwargs):
                pytest.fail("a stale plan reached the create call")

        monkeypatch.setattr(instances_router, "verify_project_access", lambda _h, p: p)
        monkeypatch.setattr(instances_router, "_run_create_preflight", fresh)
        monkeypatch.setattr(
            instances_router,
            "get_operation_job_by_request",
            lambda *_args: asyncio.sleep(0, result=None),
        )
        with pytest.raises(HTTPException) as caught:
            asyncio.run(
                instances_router.create_instance(
                    request=_request(plan_revision="a" * 64),
                    idempotency_key="stale-plan",
                    lxd_service=NeverCreate(),
                    db=SimpleNamespace(),
                    _=None,
                    _rbac=SimpleNamespace(user_id="partner", email="partner@example.test"),
                )
            )
        assert caught.value.status_code == 409
        assert caught.value.detail["code"] == "STALE_CREATE_PLAN"

    def test_networkless_create_needs_the_named_acknowledgement(self, monkeypatch):
        from app.routers import instances as instances_router
        from app.models.schemas import (
            InstanceCreateEffectiveSummary,
            InstanceCreatePreflightIssue,
            InstanceCreatePreflightResponse,
        )

        async def networkless(_request, _service):
            return CreatePreflightEvaluation(
                response=InstanceCreatePreflightResponse(
                    ready=False,
                    plan_revision="a" * 64,
                    warnings=[
                        InstanceCreatePreflightIssue(
                            code="NETWORKLESS",
                            field="devices",
                            message="isolated",
                            acknowledgement_required=True,
                        )
                    ],
                    required_acknowledgements=["networkless"],
                    effective=InstanceCreateEffectiveSummary(profiles=["default"], nic_names=[], root_device="root"),
                ),
                effective_config={},
                effective_devices={},
            )

        class NeverCreate:
            async def create_instance(self, **_kwargs):
                pytest.fail("an unacknowledged warning reached the create call")

        monkeypatch.setattr(instances_router, "verify_project_access", lambda _h, p: p)
        monkeypatch.setattr(instances_router, "_run_create_preflight", networkless)
        monkeypatch.setattr(
            instances_router,
            "get_operation_job_by_request",
            lambda *_args: asyncio.sleep(0, result=None),
        )
        with pytest.raises(HTTPException) as caught:
            asyncio.run(
                instances_router.create_instance(
                    request=_request(),
                    idempotency_key="networkless",
                    lxd_service=NeverCreate(),
                    db=SimpleNamespace(),
                    _=None,
                    _rbac=SimpleNamespace(user_id="partner", email="partner@example.test"),
                )
            )
        assert caught.value.status_code == 409
        assert caught.value.detail["code"] == "ACKNOWLEDGEMENT_REQUIRED"
        assert caught.value.detail["acknowledgements"] == ["networkless"]

    def test_create_route_is_explicitly_async_accepted(self):
        from app.routers import instances as instances_router

        route = next(
            route for route in instances_router.router.routes if route.path == "/instances" and "POST" in route.methods
        )
        assert route.status_code == 202
