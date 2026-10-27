"""What an account sees is its own organization's branding, filled from its provider's, filled from
the deployment's, key by key (services/branding/resolve.py).

The resolver is pure, so these tests hold the rules without a database: an empty value never
replaces a set one, a later layer wins, and the version moves whenever anything under it moves,
including which organization is the provider.
"""

from datetime import datetime, timezone

from app.services.branding.resolve import Layer, merge, resolve, version_token

T0 = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)

DEPLOYMENT = Layer("deployment", {
    "branding": {"appName": "Acme Cloud", "footerText": "© {YEAR} Acme"},
    "colors": {"accentColor": "#112233"},
    "theme": "light",
}, T0)
PROVIDER = Layer("provider", {
    "logo": {"lightModeLogo": "data:image/png;base64,AAAA", "darkModeLogo": None},
    "branding": {"appName": "Seller Cloud", "tagline": ""},
}, T0, organization_id=5)
OWN = Layer("organization", {"colors": {"accentColor": "#445566", "sidebarBgColor": "#000000"}}, T1, organization_id=9)


def test_a_buyer_that_changed_only_its_colours_keeps_its_sellers_logo_and_name() -> None:
    settings = merge([DEPLOYMENT, PROVIDER, OWN])

    assert settings["logo"] == {"lightModeLogo": "data:image/png;base64,AAAA"}
    assert settings["branding"]["appName"] == "Seller Cloud"
    assert settings["colors"] == {"accentColor": "#445566", "sidebarBgColor": "#000000"}


def test_an_empty_value_never_replaces_a_set_one() -> None:
    settings = merge([DEPLOYMENT, PROVIDER])

    # The provider's empty tagline and null dark logo leave the layer below alone.
    assert "tagline" not in settings["branding"]
    assert "darkModeLogo" not in settings["logo"]
    assert settings["branding"]["footerText"] == "© {YEAR} Acme"


def test_the_deployment_default_shows_where_no_organization_sets_anything() -> None:
    resolved = resolve("organization:9", [DEPLOYMENT])

    assert resolved.source == "deployment"
    assert resolved.settings["theme"] == "light"
    assert resolved.settings["branding"]["appName"] == "Acme Cloud"


def test_nothing_set_anywhere_is_the_built_in_look() -> None:
    resolved = resolve("organization:9", [])

    assert resolved.settings == {}
    assert resolved.source == "default"
    assert resolved.updated_at is None


def test_the_top_layer_names_the_source_and_the_newest_stamp_is_reported() -> None:
    resolved = resolve("organization:9", [DEPLOYMENT, PROVIDER, OWN])

    assert resolved.source == "organization"
    assert resolved.updated_at == T1


def test_the_version_moves_when_any_layer_or_the_provider_changes() -> None:
    base = version_token("organization:9", [DEPLOYMENT, PROVIDER])

    edited = Layer("provider", PROVIDER.settings, T1, organization_id=5)
    relinked = Layer("provider", PROVIDER.settings, T0, organization_id=6)
    assert version_token("organization:9", [DEPLOYMENT, edited]) != base
    assert version_token("organization:9", [DEPLOYMENT, relinked]) != base
    assert version_token("organization:9", [PROVIDER]) != base, "the deployment default was removed"
    assert version_token("organization:10", [DEPLOYMENT, PROVIDER]) != base, "another account's organization"
    assert version_token("organization:9", [DEPLOYMENT, PROVIDER]) == base
