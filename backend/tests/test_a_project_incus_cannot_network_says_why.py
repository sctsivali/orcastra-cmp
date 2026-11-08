"""A project Incus refuses for want of OVN is explained, not passed through raw.

Found on the Incus lab (Incus 7.6, no OVN): every project the create form sent with Networks ticked
(its default) answered 400 "Could not create project: OVN is required for projects with
"features.networks" enabled". Incus gives a project its own networks only through OVN; LXD
accepts the same project without it. The refusal now says what to do: untick Networks to use the
default project's networks, or set up OVN on the cluster.
"""

from app.routers.projects import project_create_refusal


def test_the_ovn_refusal_names_both_ways_forward():
    status, detail = project_create_refusal(
        'Could not create project: OVN is required for projects with "features.networks" enabled'
    )
    assert status == 422
    assert detail["code"] == "project_networks_need_ovn"
    assert "Networks" in detail["message"] and "OVN" in detail["message"]


def test_a_taken_name_is_still_a_conflict():
    assert project_create_refusal("Could not create project: Project 'web' already exists")[0] == 409


def test_any_other_refusal_keeps_its_message():
    status, detail = project_create_refusal("Could not create project: Invalid limits.memory")
    assert (status, detail) == (400, "Could not create project: Invalid limits.memory")


def test_a_project_with_no_config_gets_networks_only_where_the_engine_allows_it():
    """An API caller sending no config got features.networks=true, which Incus without OVN refuses."""
    from app.services.lxd.projects import _create_config

    lxd = _create_config(None, None, None, None)
    incus = _create_config(None, None, None, None, own_networks=False)
    assert lxd["features.networks"] == "true"
    assert "features.networks" not in incus and incus["features.images"] == "true"


def test_an_explicit_config_is_never_rewritten():
    from app.services.lxd.projects import _create_config

    assert _create_config({"features.networks": "true"}, None, None, None, own_networks=False) == {
        "features.networks": "true"
    }
