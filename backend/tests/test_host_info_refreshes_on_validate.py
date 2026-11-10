"""A pooled client's host_info must not stay frozen at connect time.

pylxd assigns host_info once in __init__ and only rewrites it inside authenticate(), and the
pool never expires a client, so a cluster upgraded in place would keep reporting its old
version. _validate_connection already performs a live GET /1.0 on every pooled get_client()
and refreshes host_info from that body. The call-count assertion keeps it to that round trip.
"""

from app.services.lxd.connection import LXDConnectionManager


class _Response:
    def __init__(self, metadata, status_code=200):
        self.status_code = status_code
        self._metadata = metadata

    def json(self):
        return {"metadata": self._metadata}


class _Api:
    def __init__(self, metadata):
        self.metadata = metadata
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return _Response(self.metadata)


class _Client:
    def __init__(self, connect_time_metadata):
        # What pylxd captured at connect time.
        self.host_info = connect_time_metadata
        self.api = _Api(connect_time_metadata)


def _manager():
    return LXDConnectionManager()


def test_host_info_is_refreshed_from_the_validation_read():
    """The cluster was upgraded; the pool must notice on its next validate."""
    stale = {"auth": "trusted", "environment": {"server_version": "5.21"}}
    client = _Client(stale)
    client.api.metadata = {"auth": "trusted", "environment": {"server_version": "6.1"}}

    assert _manager()._validate_connection(client, "nuc2") is True

    assert client.host_info["environment"]["server_version"] == "6.1"


def test_the_refresh_costs_no_extra_round_trip():
    """The refresh rides the existing validation read."""
    client = _Client({"auth": "trusted", "environment": {}})

    _manager()._validate_connection(client, "nuc2")

    assert client.api.calls == 1, (
        f"host_info refresh must ride the existing validation GET, saw {client.api.calls} calls"
    )


def test_an_untrusted_answer_is_still_rejected():
    """Refreshing must not soften the trust check that gates the pool."""
    client = _Client({"auth": "trusted", "environment": {}})
    client.api.metadata = {"auth": "untrusted", "environment": {}}

    assert _manager()._validate_connection(client, "nuc2") is False


def test_an_empty_body_leaves_the_previous_host_info_alone():
    """A known-old body is kept over an empty one: callers index into environment."""
    known = {"auth": "trusted", "environment": {"server_version": "5.21"}}
    client = _Client(known)
    client.api.metadata = {}

    _manager()._validate_connection(client, "nuc2")

    assert client.host_info == known
