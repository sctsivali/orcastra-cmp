"""Migration 045 runs the old fallback one last time, for organizations with no row of their own.

Before it, such an organization showed the personal row of whoever created it. Every personal row
carries a full settings object, mostly the defaults of the build that wrote it, so promoting all of
them would freeze a buyer founder's defaults over its seller's branding, which is the bug item 4
reported. Only a row someone actually branded is promoted. A tagline alone does not count: older
builds shipped other default taglines.
"""

import importlib.util
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _migration():
    path = next(VERSIONS.glob("*_045_branding_inheritance.py"))
    spec = importlib.util.spec_from_file_location("m045", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M045 = _migration()
DEFAULT_ROW = {
    "logo": {"lightModeLogo": None, "darkModeLogo": None, "favicon": None},
    "background": {"backgroundImage": None, "backgroundOverlayOpacity": 85, "backgroundBlur": 0},
    "branding": {"appName": "Orcastra", "tagline": "A unified dashboard for an older build.", "footerText": "© {YEAR} ORCASTRA"},
    "colors": {"accentColor": "#8467E4", "sidebarBgColor": "#321f6f", "sidebarTextColor": "#f8fafc"},
    "theme": "dark",
    "lastModified": "2026-10-01T00:00:00Z",
}


def _with(section: str, key: str, value):
    row = {name: dict(values) if isinstance(values, dict) else values for name, values in DEFAULT_ROW.items()}
    row[section][key] = value
    return row


def test_a_row_of_defaults_is_not_branded() -> None:
    assert M045._branded(DEFAULT_ROW) is False


@pytest.mark.parametrize("row", [
    _with("logo", "lightModeLogo", "data:image/png;base64,AAAA"),
    _with("background", "backgroundImage", "data:image/png;base64,AAAA"),
    _with("branding", "appName", "PARAGONCORP-Orcastra"),
    _with("branding", "footerText", "© {YEAR} Seller Cloud"),
    _with("colors", "sidebarBgColor", "#1e1b4b"),
])
def test_a_row_someone_branded_is(row) -> None:
    assert M045._branded(row) is True


def test_a_different_tagline_alone_is_not_branding() -> None:
    assert M045._branded(_with("branding", "tagline", "Something else")) is False


def test_a_promoted_row_carries_branding_and_not_the_owners_theme() -> None:
    assert set(M045._branding_only(DEFAULT_ROW)) == {"logo", "background", "branding", "colors"}
