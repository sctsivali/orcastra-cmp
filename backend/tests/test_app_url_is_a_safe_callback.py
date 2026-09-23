"""The OrcaHub callback URL policy.

Every callback carries a credential: the refresh GET carries the key's hash, the member push the
raw secret, and the webhook is signed with it. So an app_url must reach a public address over
https. The one relaxation is an operator-listed network (ex; a tailnet /32), where http is
accepted only for a literal address inside it. A hostname over http stays refused, because what
it resolves to cannot be tied to the allowed network before the request is made.
"""

import pytest

from app.core.config import get_settings
from app.services import orcahub_sync
from app.services.orcahub_sync import (
    callback_networks,
    callback_requires_https,
    check_callback_url,
)


@pytest.fixture
def allow(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setenv("ORCAHUB_CALLBACK_ALLOWED_NETWORKS", value)
        get_settings.cache_clear()
        orcahub_sync._parse_networks.cache_clear()

    monkeypatch.delenv("ORCAHUB_CALLBACK_ALLOWED_NETWORKS", raising=False)
    get_settings.cache_clear()
    orcahub_sync._parse_networks.cache_clear()
    yield _set
    monkeypatch.delenv("ORCAHUB_CALLBACK_ALLOWED_NETWORKS", raising=False)
    get_settings.cache_clear()
    orcahub_sync._parse_networks.cache_clear()


REFUSED_BY_DEFAULT = [
    "http://example.test",
    "http://93.184.216.34",
    "https://10.0.0.5",
    "https://169.254.169.254",
    "https://127.0.0.1:4050",
    "https://100.64.1.2",
    "https://[::ffff:127.0.0.1]",
    "https://user:pw@example.test",
    "https://example.test/?x=1",
    "https://example.test/#f",
    "https://example.test/?",
    "https://localhost",
    "ftp://example.test",
    "not a url",
]


@pytest.mark.parametrize("value", REFUSED_BY_DEFAULT)
def test_the_default_policy_refuses(allow, value):
    with pytest.raises(ValueError):
        check_callback_url(value)


@pytest.mark.parametrize("value", ["https://example.test", "https://example.test/base", "https://93.184.216.34:8443"])
def test_public_https_passes(allow, value):
    assert check_callback_url(value) == value


def test_an_allowed_network_admits_its_literal_over_http_and_https(allow):
    allow("10.0.0.5/32")
    assert check_callback_url("https://10.0.0.5") == "https://10.0.0.5"
    assert check_callback_url("http://10.0.0.5:3001") == "http://10.0.0.5:3001"
    with pytest.raises(ValueError):
        check_callback_url("http://10.0.0.6:3001")


def test_a_hostname_over_http_stays_refused_inside_an_allowlist(allow):
    allow("10.0.0.5/32")
    assert callback_requires_https("http://example.test") is True
    with pytest.raises(ValueError):
        check_callback_url("http://example.test")


@pytest.mark.parametrize("entry", ["0.0.0.0/0", "::/0", "169.254.0.0/16", "fd00:ec2::/64", "not-a-net"])
def test_a_too_broad_or_bad_entry_is_ignored(allow, entry):
    allow(entry)
    assert callback_networks() == ()
    with pytest.raises(ValueError):
        check_callback_url("https://10.0.0.5")


def test_a_bad_entry_does_not_drop_the_good_ones(allow):
    allow("garbage, 10.0.0.5/32 ,0.0.0.0/0")
    assert [str(n) for n in callback_networks()] == ["10.0.0.5/32"]


def test_the_metadata_address_is_refused_even_inside_an_allowed_network(allow):
    allow("169.254.169.0/24")
    with pytest.raises(ValueError):
        check_callback_url("http://169.254.169.254")


# --- the /sync boundary ---------------------------------------------------------------------

from pydantic import ValidationError  # noqa: E402

from app.routers.integrations import SyncDataRequest  # noqa: E402


@pytest.mark.parametrize("value", REFUSED_BY_DEFAULT)
def test_sync_drops_an_unsafe_app_url_and_goes_on(allow, value, caplog):
    with caplog.at_level("WARNING", logger="app.routers.integrations"):
        assert SyncDataRequest(app_url=value).app_url is None
    assert any("Ignoring the app_url of a sync" in r.getMessage() for r in caplog.records)


def test_sync_refuses_an_app_url_over_the_length_cap(allow):
    with pytest.raises(ValidationError):
        SyncDataRequest(app_url="https://example.test/" + "a" * 2048)


def test_a_dropped_app_url_leaves_the_stored_one_alone(allow, monkeypatch):
    """The handler stores app_url only when the request carries one."""
    import inspect

    from app.routers import integrations

    source = inspect.getsource(integrations.sync_data)
    assert "if request.app_url:" in source


@pytest.mark.parametrize("value", ["https://example.test", None])
def test_sync_accepts_a_public_https_app_url_or_none(allow, value):
    assert SyncDataRequest(app_url=value).app_url == value


def test_sync_without_app_url_is_unchanged(allow):
    assert SyncDataRequest().app_url is None


def test_sync_honours_the_allowlist(allow):
    allow("10.0.0.5/32")
    assert SyncDataRequest(app_url="http://10.0.0.5:3001").app_url == "http://10.0.0.5:3001"
    assert SyncDataRequest(app_url="http://example.test").app_url is None
