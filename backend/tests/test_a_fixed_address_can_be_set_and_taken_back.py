"""An operator can pin a NIC's IPv4 from the console, and hand it back to DHCP.

Chapter 5.1 of the EcoBytes audit. LXD's own web UI has no control for this, so the address of
a database node or a DNS server was decided by editing YAML on the hypervisor over SSH. Setting
it at create time is half the answer; an address planned against an IPAM catalogue also has to
be changeable on an instance that already exists.

The two things worth pinning here are the ones that are easy to get wrong: clearing an address
has to actually remove the key rather than set it empty, and a NIC inherited from a profile is
not in the instance's own devices at all, so pinning it has to write the whole device onto the
instance the way `lxc config device override` does.
"""

import pytest

from app.services.lxd.instance_devices import (
    DeviceNotFound,
    apply_fixed_address,
    nic_network_of,
    resolve_device,
    set_nic_address_sync,
)


OWN_NIC = {"type": "nic", "network": "lxdbr0", "name": "eth0"}
PROFILE_NIC = {"type": "nic", "network": "lxdbr0", "name": "eth0"}


class FakeInstanceNode:
    def __init__(self, client, name):
        self._client = client
        self._name = name

    def get(self, params=None, timeout=None):
        self._client.reads.append(dict(params or {}))
        return FakeResponse(self._client.record)

    def put(self, json=None, params=None, timeout=None):
        self._client.writes.append((json, dict(params or {})))
        return FakeResponse({})


class FakeResponse:
    def __init__(self, metadata):
        self._metadata = metadata

    def json(self):
        return {"metadata": self._metadata}


class FakeInstances:
    def __init__(self, client):
        self._client = client

    def __getitem__(self, name):
        return FakeInstanceNode(self._client, name)


class FakeApi:
    def __init__(self, client):
        self.instances = FakeInstances(client)


class FakeClient:
    def __init__(self, record):
        self.record = record
        self.reads = []
        self.writes = []
        self.api = FakeApi(self)


def record(devices=None, expanded=None, **extra):
    base = {
        "architecture": "x86_64",
        "config": {"limits.cpu": "2"},
        "devices": devices if devices is not None else {},
        "expanded_devices": expanded if expanded is not None else {},
        "profiles": ["default"],
        "description": "",
    }
    base.update(extra)
    return base


class TestResolvingTheDevice:
    def test_an_instances_own_device_is_not_inherited(self):
        device, inherited = resolve_device(record(devices={"eth0": OWN_NIC}), "eth0")
        assert device == OWN_NIC and inherited is False

    def test_a_profile_device_is_found_through_the_expanded_view(self):
        device, inherited = resolve_device(
            record(devices={}, expanded={"eth0": PROFILE_NIC}), "eth0"
        )
        assert device == PROFILE_NIC and inherited is True

    def test_a_name_that_is_not_a_device_is_reported_as_missing(self):
        with pytest.raises(DeviceNotFound):
            resolve_device(record(), "eth9")

    def test_a_nic_names_its_network_either_way(self):
        assert nic_network_of({"network": "lxdbr0"}) == "lxdbr0"
        assert nic_network_of({"parent": "br-uplink"}) == "br-uplink"
        assert nic_network_of({"type": "nic"}) is None


class TestApplyingTheAddress:
    def test_setting_an_address_adds_the_key(self):
        assert apply_fixed_address(OWN_NIC, "10.0.5.7")["ipv4.address"] == "10.0.5.7"

    @pytest.mark.parametrize("cleared", ["", "   ", None])
    def test_clearing_removes_the_key_rather_than_emptying_it(self, cleared):
        """An empty ipv4.address is not the same as no ipv4.address: LXD reads the first as a
        request it cannot satisfy. Returning to DHCP has to delete the key."""
        pinned = {**OWN_NIC, "ipv4.address": "10.0.5.7"}
        assert "ipv4.address" not in apply_fixed_address(pinned, cleared)

    def test_the_original_device_is_left_alone(self):
        original = dict(OWN_NIC)
        apply_fixed_address(original, "10.0.5.7")
        assert original == OWN_NIC


class TestWritingItBack:
    def test_the_whole_object_is_written_not_merged(self):
        """A PATCH merges, so it can add a key and never remove one. Clearing an address
        matters as much as setting it, so the write is a full PUT."""
        client = FakeClient(record(devices={"eth0": OWN_NIC}))
        set_nic_address_sync(client, "db-1", "default", "eth0", "10.0.5.7")
        payload, _params = client.writes[0]
        assert payload["devices"]["eth0"]["ipv4.address"] == "10.0.5.7"
        assert payload["config"] == {"limits.cpu": "2"}
        assert payload["profiles"] == ["default"]

    def test_an_inherited_nic_is_written_onto_the_instance(self):
        """The device is not in the instance's own map, so pinning it has to copy the whole
        expanded device across, which is what `lxc config device override` does."""
        client = FakeClient(record(devices={}, expanded={"eth0": PROFILE_NIC}))
        set_nic_address_sync(client, "db-1", "default", "eth0", "10.0.5.7")
        payload, _params = client.writes[0]
        assert payload["devices"]["eth0"] == {
            **PROFILE_NIC,
            "ipv4.address": "10.0.5.7",
        }

    def test_other_devices_are_carried_through_untouched(self):
        client = FakeClient(
            record(
                devices={
                    "eth0": OWN_NIC,
                    "root": {"type": "disk", "pool": "local", "path": "/"},
                }
            )
        )
        set_nic_address_sync(client, "db-1", "default", "eth0", "10.0.5.7")
        payload, _params = client.writes[0]
        assert payload["devices"]["root"] == {
            "type": "disk",
            "pool": "local",
            "path": "/",
        }

    def test_a_non_default_project_is_named_on_both_the_read_and_the_write(self):
        client = FakeClient(record(devices={"eth0": OWN_NIC}))
        set_nic_address_sync(client, "db-1", "UAT-Hermes", "eth0", "10.0.5.7")
        assert client.reads[0] == {"project": "UAT-Hermes"}
        assert client.writes[0][1] == {"project": "UAT-Hermes"}

    def test_the_default_project_is_left_off_the_query(self):
        client = FakeClient(record(devices={"eth0": OWN_NIC}))
        set_nic_address_sync(client, "db-1", "default", "eth0", "10.0.5.7")
        assert client.reads[0] == {}

    def test_a_device_that_is_not_a_nic_is_refused(self):
        client = FakeClient(
            record(devices={"root": {"type": "disk", "pool": "local"}})
        )
        with pytest.raises(ValueError, match="not a network interface"):
            set_nic_address_sync(client, "db-1", "default", "root", "10.0.5.7")
        assert client.writes == []

    def test_an_absent_architecture_is_not_written_as_null(self):
        """LXD refuses a PUT carrying a read-only key, and architecture is absent on some
        records rather than empty."""
        rec = record(devices={"eth0": OWN_NIC})
        rec.pop("architecture")
        client = FakeClient(rec)
        set_nic_address_sync(client, "db-1", "default", "eth0", "10.0.5.7")
        assert "architecture" not in client.writes[0][0]


# ── the write is asynchronous, and a refusal must not read as a success ────────────────────────

class _Resp:
    def __init__(self, body, status=202):
        self._body = body
        self.status_code = status
        self.headers = {}

    def json(self):
        return self._body


class _OpNode:
    def __init__(self, outcome):
        self._outcome = outcome

    @property
    def wait(self):
        return self

    def get(self, params=None, timeout=None):
        return _Resp({"metadata": self._outcome}, 200)


class _AsyncPutClient:
    """A daemon that answers the device PUT the way LXD really does: 202 plus an operation."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.waited = []
        client = self

        class Instance:
            def __init__(self, name):
                self.name = name

            def get(self, params=None, timeout=None):
                return _Resp(
                    {
                        "metadata": {
                            "devices": {"eth0": {"type": "nic", "network": "lxdbr0"}},
                            "config": {},
                            "profiles": ["default"],
                            "architecture": "x86_64",
                        }
                    },
                    200,
                )

            def put(self, json=None, params=None, timeout=None, headers=None):
                return _Resp(
                    {
                        "type": "async",
                        "operation": "/1.0/operations/abc-123",
                        "metadata": {"id": "abc-123"},
                    }
                )

        class Instances:
            def __getitem__(self, name):
                return Instance(name)

        class Operations:
            def __getitem__(self, op_id):
                client.waited.append(op_id)
                return _OpNode(client.outcome)

        class Api:
            instances = Instances()
            operations = Operations()

        self.api = Api()


class TestTheWriteIsWaitedOn:
    """LXD answers an instance PUT with 202 and does the work afterwards.

    Returning at the 202 reported a refused write as a success: the console said the address
    was reserved while the background job had failed, so the operator saw Static on a NIC that
    was still on DHCP. Verified against nuc1, whose device PUT answers
    `{"type":"async","status":"Operation created",...,"operation":"/1.0/operations/..."}`.
    """

    def test_a_successful_operation_is_waited_for(self):
        client = _AsyncPutClient({"status": "Success", "err": ""})
        set_nic_address_sync(client, "db-1", "default", "eth0", "10.0.5.42")
        assert client.waited == ["abc-123"], "the operation must be awaited, not assumed"

    def test_a_failed_operation_is_raised_rather_than_reported_as_success(self):
        client = _AsyncPutClient(
            {"status": "Failure", "err": 'Cannot specify "ipv4.address" when DHCP is disabled'}
        )
        with pytest.raises(Exception) as exc:
            set_nic_address_sync(client, "db-1", "default", "eth0", "10.0.5.42")
        assert "LXDAPIException" in type(exc.value).__name__ or "ipv4.address" in str(exc.value)
