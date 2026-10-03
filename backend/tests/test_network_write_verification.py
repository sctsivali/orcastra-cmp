"""Readback confirms explicit changes while accepting documented daemon normalization."""

import pytest
from fastapi import HTTPException

from app.services.network_write_verification import require_network_update_readback


def metadata(config=None, **overrides):
    return {"name": "qa-network", "type": "bridge", "managed": True, "status": "Created",
            "project": "default", "description": "updated", "config": config or {}, **overrides}


@pytest.mark.parametrize(("requested", "observed"), [
    ({"ipv4.address": "auto"}, {"ipv4.address": "10.42.0.1/24"}),
    ({"ipv6.address": "auto"}, {"ipv6.address": "fd42::1/64"}),
    ({"ipv6.address": "none"}, {}),
    ({"ipv6.address": "none"}, {"ipv6.address": ""}),
    ({"security.acls": "qa-allow,qa-deny"}, {"security.acls": "qa-deny,qa-allow"}),
    ({"dns.domain": "QA.INVALID."}, {"dns.domain": "qa.invalid"}),
    ({"bridge.mtu": ""}, {"bridge.mtu": "1500"}),
])
def test_expected_daemon_normalization_confirms_the_write(requested, observed):
    result = require_network_update_readback(metadata(observed), "qa-network", "bridge", "default", requested, "updated")
    assert result["name"] == "qa-network"


@pytest.mark.parametrize(("requested", "observed"), [
    ({"bridge.mtu": "1500"}, {"bridge.mtu": "1400"}),
    ({"ipv4.address": "auto"}, {"ipv4.address": "none"}),
    ({"ipv4.address": "auto"}, {"ipv4.address": "fd42::1/64"}),
    ({"ipv6.address": "auto"}, {"ipv6.address": "1:2/64"}),
    ({"ipv6.address": "none"}, {"ipv6.address": "fd42::1/64"}),
    ({"ipv6.address": "none"}, {"ipv6.address": []}),
    ({"security.acls": "qa-allow"}, {"security.acls": "qa-neighbor"}),
    ({"dns.domain": "qa.invalid"}, {"dns.domain": "neighbor.invalid"}),
])
def test_unconfirmed_overrides_are_not_a_success(requested, observed):
    with pytest.raises(HTTPException) as error:
        require_network_update_readback(metadata(observed), "qa-network", "bridge", "default", requested, None)
    assert error.value.status_code == 502


def test_description_readback_must_match_the_requested_change():
    with pytest.raises(HTTPException) as error:
        require_network_update_readback(metadata(), "qa-network", "bridge", "default", None, "unapplied")
    assert error.value.status_code == 502
