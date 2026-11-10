"""The migration source URL and certificate come from the server, never from the caller.

A pull migration tells the DESTINATION hypervisor to open a connection to an address and trust a
certificate. If either could be supplied in the request body, an authenticated partner would gain
an SSRF primitive aimed from inside a hypervisor, which reaches far more than the API server can.
`_validate_image_server` in the instances router guards the image path for the same reason.

Also covered here: the import POST must not travel on a session that retries, because a retried
instance import creates the instance twice, and for a multi-gigabyte transfer that means two
half-built instances and a filled pool.
"""

import inspect

import pytest

from app.routers import instance_mobility
from app.services.lxd import mobility


def test_the_request_model_offers_no_source_url_or_certificate():
    fields = set(instance_mobility.MoveRequest.model_fields)
    for forbidden in ("source_url", "operation", "operation_url", "certificate", "secrets", "endpoint"):
        assert forbidden not in fields, (
            f"MoveRequest must not accept '{forbidden}': it would let a caller point a "
            f"hypervisor at an address of their choosing"
        )


def test_the_certificate_is_read_from_the_source_servers_own_info():
    source = inspect.getsource(mobility.server_certificate)
    assert "host_info" in source, "the certificate must come from the connected source server"

    class _Client:
        host_info = {"environment": {"certificate": "-----BEGIN CERTIFICATE-----\nx\n-----END"}}

    assert mobility.server_certificate(_Client()).startswith("-----BEGIN CERTIFICATE-----")


def test_a_source_without_a_certificate_fails_closed():
    class _Client:
        host_info = {"environment": {}}

    try:
        mobility.server_certificate(_Client())
    except mobility.MobilityError:
        return
    raise AssertionError("a source with no certificate must refuse, not migrate unverified")


def test_the_import_session_does_not_retry():
    class _Pooled:
        cert = ("/tmp/c.crt", "/tmp/c.key")
        verify = False
        headers = {}

    class _Client:
        class api:
            session = _Pooled()

    session = mobility.non_retrying_session(_Client())
    for prefix in ("http://", "https://"):
        adapter = session.get_adapter(prefix + "example.invalid")
        assert adapter.max_retries.total == 0, (
            "a retried import POST creates the instance twice"
        )


def test_transport_selection_prefers_native_migration_across_clusters():
    assert mobility.choose_transport("nuc2", "nuc2") == mobility.TRANSPORT_COPY
    assert mobility.choose_transport("nuc2", "panda") == mobility.TRANSPORT_MIGRATION


def test_only_a_reachability_failure_falls_back_to_streaming():
    # A permissions or state failure must surface as itself. Downgrading it to the slow path
    # would fail the same way minutes later, having consumed pool space in the meantime.
    assert mobility.is_unreachable_error(Exception("dial tcp 10.0.0.1:8443: connection refused"))
    assert mobility.is_unreachable_error(Exception("no route to host"))
    assert not mobility.is_unreachable_error(Exception("not authorized"))
    assert not mobility.is_unreachable_error(Exception("Instance is running"))


def test_an_incompatible_architecture_is_refused_rather_than_offered():
    """A destination that cannot run the instance is never presented as a choice.

    An x86_64 instance cannot run on an aarch64 host however the transfer is arranged. Offering
    that destination and failing afterwards would read as a broken feature.
    """
    assert mobility.architecture_compatible("x86_64", ["aarch64", "armv7l"]) is False
    assert mobility.architecture_compatible("x86_64", ["x86_64", "i686"]) is True
    assert mobility.architecture_compatible("aarch64", ["aarch64", "armv8l"]) is True


def test_an_unknown_architecture_does_not_block_a_move():
    # Refusing on a failed read would block a legitimate copy because a probe failed. LXD still
    # has the final say when the copy is attempted.
    assert mobility.architecture_compatible(None, ["x86_64"]) is True
    assert mobility.architecture_compatible("x86_64", []) is True


@pytest.mark.parametrize("instance_type", ["container", "virtual-machine"])
def test_a_migration_declares_the_instance_architecture_and_type(instance_type):
    """The migration body must name the architecture, or the target refuses it outright.

    A same-cluster copy infers it from the source; a migration target builds the instance record
    before any bytes move and has nothing to infer from. Without it LXD answers
    `Architecture is not supported: ""`.
    """
    captured = {}

    class _Node:
        def post(self, json=None, params=None):
            captured.update(json or {})

            class _R:
                @staticmethod
                def json():
                    return {"operation": "/1.0/operations/op"}

            return _R()

    class _Client:
        class api:
            instances = _Node()

    mobility.accept_migration_on_target(
        _Client(),
        target_name="copy",
        target_project="p",
        source_operation_url="https://source/1.0/operations/x",
        source_certificate="cert",
        secrets={"control": "c"},
        definition={
            "type": instance_type,
            "config": {"limits.cpu": "1"},
            "devices": {"root": {"path": "/", "type": "disk"}},
            "profiles": ["default"],
            "architecture": "x86_64",
        },
    )
    assert captured["architecture"] == "x86_64"
    assert captured["type"] == instance_type
    assert captured["config"]["limits.cpu"] == "1"
    assert captured["source"]["mode"] == "pull"


def test_the_migration_operation_url_is_absolute():
    """The URL handed to the target must name a host it can open.

    LXD answers a migration request with a relative path. That path is consumed by the TARGET,
    which has no host to resolve it against, so it accepts the request and then does nothing:
    the operation is created, no bytes move, and the instance never appears. The copy still
    answers 202, so nothing in the response distinguishes that from success.
    """

    class _Instance:
        def post(self, json=None, params=None):
            class _R:
                @staticmethod
                def json():
                    return {
                        "operation": "/1.0/operations/abc-123",
                        "metadata": {"metadata": {"control": "c", "fs": "f"}},
                    }

            return _R()

    class _Instances:
        def __getitem__(self, name):
            return _Instance()

    class _Client:
        class api:
            instances = _Instances()
            _api_endpoint = "https://100.64.12.102:8443/1.0"

    url, secrets = mobility.begin_source_migration(
        _Client(), instance_name="ct-x", project="p"
    )
    assert url == "https://100.64.12.102:8443/1.0/operations/abc-123", url
    assert url.startswith("https://"), "a relative path leaves the target nothing to resolve"
    assert secrets == {"control": "c", "fs": "f"}


def test_an_absolute_url_from_another_origin_is_refused():
    class _Instance:
        def post(self, json=None, params=None):
            class _R:
                @staticmethod
                def json():
                    return {
                        "operation": "https://elsewhere:8443/1.0/operations/z",
                        "metadata": {"metadata": {"control": "c"}},
                    }

            return _R()

    class _Instances:
        def __getitem__(self, name):
            return _Instance()

    class _Client:
        class api:
            instances = _Instances()
            _api_endpoint = "https://100.64.12.102:8443/1.0"

    try:
        mobility.begin_source_migration(_Client(), instance_name="ct-x", project="p")
    except mobility.MobilityError as exc:
        assert "outside its registered endpoint" in str(exc)
        return
    raise AssertionError("a daemon-supplied foreign operation URL must fail closed")


def test_an_absolute_url_on_the_registered_origin_is_allowed():
    class _Instance:
        def post(self, json=None, params=None):
            class _R:
                @staticmethod
                def json():
                    return {
                        "operation": "https://100.64.12.102:8443/1.0/operations/z",
                        "metadata": {"metadata": {"control": "c"}},
                    }

            return _R()

    class _Instances:
        def __getitem__(self, name):
            return _Instance()

    class _Client:
        class api:
            instances = _Instances()
            _api_endpoint = "https://100.64.12.102:8443/1.0"

    url, _ = mobility.begin_source_migration(_Client(), instance_name="ct-x", project="p")
    assert url == "https://100.64.12.102:8443/1.0/operations/z"


def test_operation_url_rejects_query_fragment_credentials_and_wrong_path():
    class _Client:
        class api:
            _api_endpoint = "https://source.example:8443/1.0"

    invalid = (
        "https://source.example:8443/1.0/operations/z?next=http://internal",
        "https://source.example:8443/1.0/operations/z#fragment",
        "https://user@source.example:8443/1.0/operations/z",
        "https://source.example:8443/1.0/images/z",
        "ftp://source.example:8443/1.0/operations/z",
    )
    for value in invalid:
        try:
            mobility.validated_operation_url(_Client(), value)
        except mobility.MobilityError:
            continue
        raise AssertionError(f"unsafe migration operation URL was accepted: {value}")
