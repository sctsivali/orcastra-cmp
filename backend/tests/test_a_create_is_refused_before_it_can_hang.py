"""An instance create must be refused at the boundary, not left spinning.

Picking a network the target project cannot reach produced an LXD create that
was accepted as an async operation and then failed in the background. The console's placeholder
row has no path to learn that, so the instance sat at "Creating..." with empty IP, Profiles and
Root Disk columns until someone went looking in the daemon log. The right behaviour is the
opposite: refuse it up front with a reason, or succeed.

The two cases a naive version of this check misses are pinned first, because they are the ones
the field reports: the NIC normally arrives from a profile rather than from the request,
and it can name its network with `parent` instead of `network`.
"""

import pytest

from app.services.lxd.nic_preflight import (
    NicPreflightError,
    check_nics,
    collect_nics,
)

DEFAULT_PROJECT = "default"


class FakeNode:
    def __init__(self, client, collection):
        self._client = client
        self._collection = collection

    def get(self, params=None, timeout=None):
        params = params or {}
        project = params.get("project", DEFAULT_PROJECT)
        if self._collection == "projects":
            payload = [
                {"name": n, "config": c} for n, c in self._client.projects.items()
            ]
        elif self._collection == "networks":
            payload = self._client.networks.get(project, [])
        else:
            payload = self._client.profiles.get(project, [])
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


class FakeClient:
    def __init__(self, projects=None, networks=None, profiles=None):
        self.projects = projects or {DEFAULT_PROJECT: {}}
        self.networks = networks or {}
        self.profiles = profiles or {}
        self.reads = []
        self.project = None
        self.api = FakeApi(self)


def bridge(name, subnet="10.0.5.1/24", managed=True):
    return {"name": name, "managed": managed, "config": {"ipv4.address": subnet}}


ISOLATED = {
    DEFAULT_PROJECT: {"features.networks": "true", "features.profiles": "true"},
    "UAT-Hermes": {"features.networks": "true", "features.profiles": "true"},
}


def client_with_isolated_project():
    return FakeClient(
        projects=ISOLATED,
        networks={
            DEFAULT_PROJECT: [bridge("br-uplink"), bridge("lxdbr0")],
            "UAT-Hermes": [bridge("ovn-uat-hermes", "192.168.3.1/24")],
        },
        profiles={
            DEFAULT_PROJECT: [{"name": "default", "devices": {}}],
            "UAT-Hermes": [{"name": "default", "devices": {}}],
        },
    )


class TestTheCasesTheAuditFiled:
    def test_a_network_from_another_project_is_refused(self):
        """The sequence as reported: create in UAT-Hermes, pick br-uplink from the global
        list the form offered, watch it hang."""
        client = client_with_isolated_project()
        with pytest.raises(NicPreflightError) as exc:
            check_nics(
                client,
                "UAT-Hermes",
                {"eth0": {"type": "nic", "network": "br-uplink"}},
                ["default"],
            )
        assert "br-uplink" in str(exc.value)
        assert "does not exist in project 'UAT-Hermes'" in str(exc.value)

    def test_the_refusal_names_what_the_project_can_actually_use(self):
        client = client_with_isolated_project()
        with pytest.raises(NicPreflightError) as exc:
            check_nics(
                client, "UAT-Hermes", {"eth0": {"type": "nic", "network": "lxdbr0"}}, ["default"]
            )
        assert "ovn-uat-hermes" in str(exc.value)

    def test_the_project_s_own_network_is_accepted(self):
        client = client_with_isolated_project()
        check_nics(
            client,
            "UAT-Hermes",
            {"eth0": {"type": "nic", "network": "ovn-uat-hermes"}},
            ["default"],
        )

    def test_a_nic_inherited_from_a_profile_is_checked_too(self):
        """The console submits profiles: ["default"] and lets the profile supply eth0, so a
        check that only reads the request passes exactly the case that hangs."""
        client = FakeClient(
            projects=ISOLATED,
            networks={
                DEFAULT_PROJECT: [bridge("br-uplink")],
                "UAT-Hermes": [bridge("ovn-uat-hermes", "192.168.3.1/24")],
            },
            profiles={
                "UAT-Hermes": [
                    {
                        "name": "default",
                        "devices": {"eth0": {"type": "nic", "network": "br-uplink"}},
                    }
                ]
            },
        )
        with pytest.raises(NicPreflightError) as exc:
            check_nics(client, "UAT-Hermes", None, ["default"])
        assert "br-uplink" in str(exc.value)

    def test_a_nic_naming_its_network_with_parent_is_checked_too(self):
        """A bare `parent`, with no nictype, is still read as a network reference."""
        client = client_with_isolated_project()
        with pytest.raises(NicPreflightError):
            check_nics(
                client,
                "UAT-Hermes",
                {"eth0": {"type": "nic", "parent": "br-uplink"}},
                ["default"],
            )

    @pytest.mark.parametrize(
        "nictype", ["macvlan", "physical", "sriov", "ipvlan", "routed", "bridged"]
    )
    def test_a_parent_that_names_a_host_interface_is_not_a_missing_network(self, nictype):
        """The regression this pins broke instance create on a stock LXD host.

        A default profile hands out `eth0` as macvlan on a physical parent. `enp86s0` is listed
        by /1.0/networks with managed:false, so reading `parent` as a network reference made it
        resolve and then fail the managed test, refusing a create the daemon would have taken.
        LXD resolves a host parent itself, so there is nothing here to refuse.
        """
        client = client_with_isolated_project()
        check_nics(
            client,
            "UAT-Hermes",
            {"eth0": {"type": "nic", "nictype": nictype, "parent": "enp86s0"}},
            ["default"],
        )

    def test_an_inherited_macvlan_nic_does_not_block_a_create(self):
        """The exact shape nuc1's default profile carries."""
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
        """The reported case must keep failing: `network=` is a managed reference."""
        client = client_with_isolated_project()
        with pytest.raises(NicPreflightError):
            check_nics(
                client,
                "UAT-Hermes",
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
    """Ch 5.1: a NIC may pin its IPv4, and a bad one must not reach the daemon."""

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
            check_nics(
                client, None, {"eth0": {"type": "nic", "network": "docker0"}}, ["default"]
            )
        assert "does not manage" in str(exc.value)

    def test_a_create_with_no_nic_at_all_is_left_alone(self):
        check_nics(client_with_isolated_project(), "UAT-Hermes", {}, ["default"])

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

    A fail-fast that arrives as a 500 or a 503 is no better than the hang it replaced: the
    console renders one as an outage and the other as "try again later", and neither tells the
    operator to pick a different network. So the wording of these refusals is load-bearing.
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
            networks={DEFAULT_PROJECT: [bridge("br-uplink")], "UAT-Hermes": []},
            profiles={"UAT-Hermes": [{"name": "default", "devices": {}}]},
        )
        subnet_client = FakeClient(
            projects={DEFAULT_PROJECT: {}},
            networks={DEFAULT_PROJECT: [bridge("lxdbr0", "10.0.5.1/24")]},
            profiles={DEFAULT_PROJECT: [{"name": "default", "devices": {}}]},
        )
        cases = [
            (client, "UAT-Hermes", {"eth0": {"type": "nic", "network": "br-uplink"}}),
            (empty, "UAT-Hermes", {"eth0": {"type": "nic", "network": "br-uplink"}}),
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
