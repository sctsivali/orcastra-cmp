"""An image built without a stamp answers null, not the empty string.

`routers/system.py` read the two provenance fields through `getattr(settings, 'build_date', None)`,
written when they did not exist on Settings yet. Once they landed carrying `default=""` the getattr
stopped being a fallback and started returning the empty string, so `/system/version` answered `""`
where a client testing `if (build_commit)` sees a value and one testing `!== null` sees another.
Both readings are wrong about the same deployment.

Reading through one helper fixes it in every consumer at once, which is the point of the helper.

Also pinned here: `API_VERSION`. Compose has always passed it and `.env.example` has always
described it as the version the API reports, but nothing declared it on Settings, so `extra = ignore`
dropped it and both `/system` endpoints the frontend reads it from answered without it. The frontend
types it as a required string on each, so it was undefined at runtime on a documented contract.

Hermetic: settings are stubbed in place, so no environment, no database and no network.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import build_identity  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.routers.system import SystemSettings, VersionInfo  # noqa: E402


def _stub(monkeypatch, **values) -> None:
    monkeypatch.setattr(build_identity, "get_settings", lambda: SimpleNamespace(**values))


def test_an_unstamped_build_reads_as_null(monkeypatch):
    _stub(monkeypatch, app_version="", build_commit="", build_date="")
    assert build_identity.version() is None
    assert build_identity.commit() is None
    assert build_identity.built_at() is None
    assert build_identity.full() == {"version": None, "commit": None, "built_at": None}


def test_whitespace_is_not_a_stamp(monkeypatch):
    """A build argument that was passed but left blank arrives as spaces, not as an empty string."""
    _stub(monkeypatch, app_version="   ", build_commit="\t", build_date=" ")
    assert build_identity.full() == {"version": None, "commit": None, "built_at": None}


def test_a_stamped_build_is_reported_verbatim(monkeypatch):
    _stub(monkeypatch, app_version="1.0.0-RC4", build_commit="abc1234", build_date="2026-09-03T00:00:00Z")
    assert build_identity.full() == {
        "version": "1.0.0-RC4",
        "commit": "abc1234",
        "built_at": "2026-09-03T00:00:00Z",
    }


def test_the_version_endpoint_can_hold_a_null_provenance() -> None:
    """The model must accept what an unstamped build produces, or the fix trades one fault for a 500."""
    info = VersionInfo(app_version="", api_version="", build_date=None, build_commit=None, environment="production")
    assert info.build_date is None
    assert info.build_commit is None


def test_api_version_is_declared_so_the_environment_reaches_the_response() -> None:
    """Compose passes API_VERSION and .env.example documents it. Before it was declared,
    `extra = ignore` dropped it and both endpoints answered without the field."""
    assert Settings(API_VERSION="9.9.9").api_version == "9.9.9"


def test_both_system_endpoints_carry_the_api_version() -> None:
    """The frontend types it as a required string on each of them."""
    assert "api_version" in VersionInfo.model_fields
    assert "api_version" in SystemSettings.model_fields
