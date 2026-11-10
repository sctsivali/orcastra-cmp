"""A cluster whose resources read timed out has not stopped trusting the console.

derive_status maps an answering host without resources to NOT_AUTHORIZED with the trust-add
hint, so only a 401 or 403 on the resources read may count as a refusal. A timeout read as
one would tell the operator to re-trust a certificate that is fine.
"""

from types import SimpleNamespace

import pytest
from pylxd.exceptions import LXDAPIException
from requests import exceptions as requests_exc

from app.models.cluster_status import ClusterStatus, derive_status
from app.services.lxd import resources as resources_module
from app.services.lxd.freshness import resources_failure
from app.services.lxd.resources import ResourceManager


def _api_error(status: int) -> LXDAPIException:
    response = SimpleNamespace(status_code=status, json=lambda: {"error": "refused"}, text="refused")
    return LXDAPIException(response)


@pytest.mark.parametrize(
    "error, reachable, words",
    [
        (requests_exc.ReadTimeout("read timed out"), False, "Did not answer"),
        (requests_exc.ConnectionError("connection reset"), False, "Did not answer"),
        (_api_error(403), True, "Not authorized"),
        (_api_error(401), True, "Not authorized"),
        (_api_error(500), False, "HTTP 500"),
        (RuntimeError("Resources read for c returned HTTP 502"), False, "Answered without its resources"),
    ],
)
def test_only_a_refusal_reads_as_not_authorized(error, reachable, words):
    is_reachable, message = resources_failure(error)

    assert is_reachable is reachable and words in message
    status, _, shown = derive_status(connected=False, reachable=is_reachable, auth="trusted", error=message)
    if reachable:
        assert status is ClusterStatus.NOT_AUTHORIZED
    else:
        assert status is ClusterStatus.OFFLINE and shown == message


def test_the_status_check_carries_the_honest_reading(monkeypatch):
    def times_out(client, node_name):
        raise requests_exc.ReadTimeout("read timed out")

    monkeypatch.setattr(resources_module, "read_resources", times_out)
    monkeypatch.setattr(
        resources_module,
        "get_node_metadata_store",
        lambda: SimpleNamespace(get_node_display_name=lambda name: name, get_all_node_metadata=dict),
    )
    client = SimpleNamespace(host_info={"auth": "trusted", "environment": {"server": "lxd"}})

    status = ResourceManager().check_node_status(client, "busy-cluster", "https://busy.invalid:8443", False)

    assert status.connected is False and status.reachable is False
    assert "Did not answer the resources read" in status.error
    assert "Not authorized" not in status.error
