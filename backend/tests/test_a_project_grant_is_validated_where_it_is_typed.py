"""What a client may put in an API key's project allow-list.

The list used to accept anything. A name is compared literally against the project LXD reports
at every enforcement site, so `prodd` for `prod` produced a key that authenticated, showed the
right badge on its card, and was refused by every project-bounded call for the rest of its life.
Nothing anywhere said so. The console now picks these from the cluster's real project list, and
this is the same guarantee for anything that posts to the API directly.

Two of these tests exist because the obvious "cleanups" are privilege changes:

  NFKC folding maps U+FF0A FULLWIDTH ASTERISK onto `*`, and `*` is the cluster-wide wildcard, so
  a tidy-up pass would manufacture a full grant out of one character.

  Case folding looks harmless and is the defect itself: `Tenant-A` folded to `tenant-a` names
  nothing on a cluster that spells it the first way. Both spellings exist on this fleet today.
"""

import pytest
from pydantic import ValidationError

from app.core.api_key_capabilities import (
    MAX_PROJECTS_PER_GRANT,
    PROJECT_NAME_MAX_LENGTH,
    normalise_project_grant,
)
from app.routers.integrations import ClusterPermission, ClusterPermissionRequest

CLUSTER = "nuc2"


def _grant(projects):
    return ClusterPermissionRequest(cluster_id=CLUSTER, permissions=["read"], projects=projects)


REFUSED = [
    ("a space", ["tenant a"]),
    ("a slash, which would reach another path", ["../etc"]),
    ("a query separator", ["default?project=other"]),
    ("a percent-encoded separator", ["default%2f.."]),
    ("an interior newline", ["default\nevil"]),
    ("only whitespace", ["   "]),
    ("a leading hyphen", ["-archive"]),
    ("a leading dot", [".hidden"]),
    ("non-ascii latin", ["defaułt"]),
    ("a cyrillic homoglyph", ["tenant-а"]),
    ("a zero-width space", ["tenant​-a"]),
]


@pytest.mark.parametrize("label,projects", REFUSED, ids=[case[0] for case in REFUSED])
def test_a_project_name_is_refused(label, projects):
    with pytest.raises(ValidationError):
        _grant(projects)


def test_a_fullwidth_asterisk_is_refused_rather_than_folded_into_a_wildcard():
    """The highest-value test here. Folding it would widen the grant to the whole cluster."""
    with pytest.raises(ValidationError):
        _grant(["＊"])


def test_case_is_preserved_because_enforcement_compares_exactly():
    """This fleet really does carry `Ryo-Labs` on one cluster and `ryo-labs` on another."""
    assert _grant(["Ryo-Labs"]).projects == ["Ryo-Labs"]
    assert _grant(["Ryo-Labs", "ryo-labs"]).projects == ["Ryo-Labs", "ryo-labs"]


def test_surrounding_whitespace_is_trimmed():
    assert _grant(["  staging  "]).projects == ["staging"]


def test_duplicates_are_removed_preserving_the_order_the_operator_picked():
    assert _grant(["staging", "default", "staging"]).projects == ["staging", "default"]


def test_a_blank_entry_is_dropped_rather_than_stored():
    assert _grant(["default", "", "   ", "staging"]).projects == ["default", "staging"]


def test_length_is_measured_before_trimming():
    """Otherwise a name could launder its length through surrounding spaces."""
    at_the_limit = "a" * PROJECT_NAME_MAX_LENGTH
    assert _grant([at_the_limit]).projects == [at_the_limit]
    with pytest.raises(ValidationError):
        _grant(["  " + at_the_limit + "  "])
    with pytest.raises(ValidationError):
        _grant(["a" * (PROJECT_NAME_MAX_LENGTH + 1)])


def test_an_empty_project_list_is_refused_at_the_boundary():
    """Refused, never quietly widened to ["default"].

    An empty list denies every project at runtime, so turning it into the default project would
    grant more than the caller asked for. The console used to do exactly that with an empty
    text box, which is half of the defect this change removes.
    """
    with pytest.raises(ValidationError):
        _grant([])
    with pytest.raises(ValidationError):
        _grant(["", "  "])


def test_an_omitted_projects_field_still_defaults_to_the_narrow_one():
    assert ClusterPermissionRequest(cluster_id=CLUSTER).projects == ["default"]


def test_a_wildcard_alone_is_accepted():
    assert _grant(["*"]).projects == ["*"]


def test_a_wildcard_mixed_with_named_projects_is_refused():
    """`_granted_projects` returns the unrestricted wildcard the moment `*` appears.

    So a stored ["tenant-a", "*"] reads on every screen as scoped to one project while granting
    the whole cluster. Refusing the mix forces the caller to state which one they meant.
    """
    with pytest.raises(ValidationError):
        _grant(["tenant-a", "*"])
    with pytest.raises(ValidationError):
        _grant(["*", "tenant-a"])


def test_a_grant_may_not_name_more_projects_than_a_cluster_plausibly_holds():
    assert len(_grant([f"p{i}" for i in range(MAX_PROJECTS_PER_GRANT)]).projects) == (
        MAX_PROJECTS_PER_GRANT
    )
    with pytest.raises(ValidationError):
        _grant([f"p{i}" for i in range(MAX_PROJECTS_PER_GRANT + 1)])


def test_a_non_string_entry_is_refused():
    with pytest.raises(ValidationError):
        ClusterPermissionRequest(cluster_id=CLUSTER, projects=[{"name": "default"}])


def test_the_stored_shape_is_unchanged_by_validation():
    """The subclass adds validators and no fields, so Vault sees exactly what it saw before."""
    dumped = _grant(["default"]).model_dump()
    assert dumped == ClusterPermission(**dumped).model_dump()
    assert set(dumped) == {"cluster_id", "cluster_name", "permissions", "projects"}


def test_the_helper_refuses_a_value_that_is_not_a_list():
    with pytest.raises(ValueError):
        normalise_project_grant("default")


def test_the_read_model_does_not_run_any_of_this():
    """Storage stays lenient. See test_a_legacy_grant_still_lists_without_a_500.py."""
    assert ClusterPermission(cluster_id=CLUSTER, projects=["  Tenant A  "]).projects == [
        "  Tenant A  "
    ]


# ---------------------------------------------------------------------------
# The existence check: strict where it can prove the negative, quiet where it cannot.
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

from fastapi import HTTPException  # noqa: E402

import app.routers.integrations as mod  # noqa: E402


def _project_rows(mapping):
    return [
        {"name": name, "cluster_id": cluster_id, "description": None}
        for cluster_id, names in mapping.items()
        for name in names
    ]


def _fanout(reachable, silent=()):
    calls = []

    async def fake(fn, ctx, cluster_ids, label, *extra):
        calls.append(list(cluster_ids))
        return mod.ClusterFanout(
            _project_rows(reachable),
            [{"cluster_id": cid, "reason": "connection refused"} for cid in silent],
        )

    return fake, calls


def _check(permissions, reachable, silent=(), monkeypatch=None):
    fake, calls = _fanout(reachable, silent)
    monkeypatch.setattr(mod, "_gather_per_cluster", fake)
    asyncio.run(mod._assert_granted_projects_exist(object(), permissions))
    return calls


def test_a_project_every_answering_cluster_has_is_accepted(monkeypatch):
    calls = _check(
        [_grant(["default"])], {CLUSTER: ["default", "staging"]}, monkeypatch=monkeypatch
    )
    assert calls == [[CLUSTER]]


def test_a_project_no_answering_cluster_has_is_refused(monkeypatch):
    fake, _ = _fanout({CLUSTER: ["default", "staging"]})
    monkeypatch.setattr(mod, "_gather_per_cluster", fake)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(mod._assert_granted_projects_exist(object(), [_grant(["stagng"])]))
    assert caught.value.status_code == 422
    assert "stagng" in caught.value.detail


def test_the_refusal_does_not_leak_the_cluster_real_project_names(monkeypatch):
    """A rejected write must not become a way for a partner to enumerate another tenant."""
    fake, _ = _fanout({CLUSTER: ["cust-acme-1234", "cust-globex-5678"]})
    monkeypatch.setattr(mod, "_gather_per_cluster", fake)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(mod._assert_granted_projects_exist(object(), [_grant(["typo"])]))
    assert "cust-acme-1234" not in caught.value.detail
    assert "cust-globex-5678" not in caught.value.detail


def test_a_project_on_only_one_of_the_selected_clusters_is_accepted(monkeypatch):
    """The union model. The console fans one list out to every cluster and warns about the gap.

    Refusing here would make a legitimate multi-cluster grant impossible whenever the clusters
    do not hold identical project sets, which on a real fleet is always.
    """
    permissions = [
        ClusterPermissionRequest(cluster_id="nuc2", projects=["SCT-OPS"]),
        ClusterPermissionRequest(cluster_id="central2", projects=["SCT-OPS"]),
    ]
    _check(
        permissions,
        {"nuc2": ["SCT-OPS", "default"], "central2": ["default"]},
        monkeypatch=monkeypatch,
    )


def test_an_unknown_name_is_accepted_when_a_selected_cluster_stayed_silent(monkeypatch):
    """Inconclusive is not guilty.

    Narrowing an over-broad key is exactly what an operator does during an outage, so a cluster
    being down must never be the reason a grant is refused.
    """
    permissions = [
        ClusterPermissionRequest(cluster_id="nuc2", projects=["only-on-the-dead-one"]),
        ClusterPermissionRequest(cluster_id="blitz001", projects=["only-on-the-dead-one"]),
    ]
    _check(
        permissions,
        {"nuc2": ["default"]},
        silent=["blitz001"],
        monkeypatch=monkeypatch,
    )


def test_nothing_is_checked_when_no_selected_cluster_answered(monkeypatch):
    _check(
        [_grant(["anything-at-all"])], {}, silent=[CLUSTER], monkeypatch=monkeypatch
    )


def test_a_wildcard_grant_never_reaches_the_cluster(monkeypatch):
    """No fan-out at all: `*` names no project, so there is nothing to look up."""
    calls = _check([_grant(["*"])], {}, monkeypatch=monkeypatch)
    assert calls == []


def test_the_check_runs_after_the_cluster_scope_guard():
    """Structural. It reaches the clusters, so it must not run for one the caller was refused."""
    import inspect

    source = inspect.getsource(mod.create_api_key)
    guard = source.index("Not permitted to grant access to clusters")
    check = source.index("_assert_granted_projects_exist")
    assert guard < check

    source = inspect.getsource(mod.update_api_key_permissions)
    guard = source.index("Not permitted to grant access to clusters")
    check = source.index("_assert_granted_projects_exist")
    assert guard < check
