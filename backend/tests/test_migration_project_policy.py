"""How a migration reproduces and compares project configuration."""

from __future__ import annotations

import copy
from typing import Any, Optional

import pytest

from app.services.migrations.project_policy import (
    LXD_DEFAULT_FEATURES,
    MIGRATION_JOB_MARKER,
    clone_config,
    created_by_job,
    features_enabled,
    policy_config,
    policy_fingerprint,
    projects_equivalent,
)


def _project(config: Optional[dict[str, Any]] = None, name: str = "tenant-a") -> dict[str, Any]:
    return {"name": name, "config": dict(config or {})}


SOURCE = _project(
    {
        "features.images": "false",
        "features.networks": "true",
        "restricted": "true",
        "limits.cpu": "8",
        "user.note": "billing team",
        "user.orcastra.creation_id": "create-123",
    }
)


def test_policy_config_drops_every_user_key() -> None:
    assert policy_config(SOURCE["config"]) == {
        "features.images": "false",
        "features.networks": "true",
        "restricted": "true",
        "limits.cpu": "8",
    }


def test_a_clone_keeps_operator_metadata_but_not_control_plane_markers() -> None:
    config = clone_config(SOURCE, job_id=None)

    assert config["user.note"] == "billing team"
    assert "user.orcastra.creation_id" not in config
    assert MIGRATION_JOB_MARKER not in config


def test_a_clone_states_lxd_default_features_without_overriding_explicit_values() -> None:
    config = clone_config(SOURCE, job_id=None)

    assert config["features.images"] == "false"
    for feature in LXD_DEFAULT_FEATURES:
        if feature != "features.images":
            assert config[feature] == "true"


def test_a_clone_carries_the_creating_job_marker() -> None:
    config = clone_config(SOURCE, job_id="job-42")

    assert config[MIGRATION_JOB_MARKER] == "job-42"
    assert created_by_job({"config": config}, "job-42") is True


@pytest.mark.parametrize("project", [{}, {"config": None}, {"config": "not-a-mapping"}])
def test_a_clone_of_a_project_without_config_is_only_the_lxd_defaults(
    project: dict[str, Any],
) -> None:
    assert clone_config(project, job_id=None) == {
        feature: "true" for feature in LXD_DEFAULT_FEATURES
    }


def test_a_clone_does_not_mutate_its_source() -> None:
    before = copy.deepcopy(SOURCE)

    clone_config(SOURCE, job_id="job-42")

    assert SOURCE == before


def test_projects_differing_only_in_user_keys_are_equivalent() -> None:
    target = _project(
        {**policy_config(SOURCE["config"]), "user.note": "other", MIGRATION_JOB_MARKER: "job-9"}
    )

    assert projects_equivalent(SOURCE, target) is True
    assert policy_fingerprint(SOURCE) == policy_fingerprint(target)


def test_an_explicit_default_feature_matches_a_project_that_omits_it() -> None:
    explicit = _project({"features.profiles": "true", "restricted": "true"})
    implicit = _project({"restricted": "true"})

    assert projects_equivalent(explicit, implicit) is True
    assert policy_fingerprint(explicit) == policy_fingerprint(implicit)


@pytest.mark.parametrize(
    "change",
    [
        {"restricted": "false"},
        {"limits.cpu": "16"},
        {"features.images": "true"},
        {"restricted.containers.nesting": "allow"},
    ],
)
def test_any_policy_difference_breaks_equivalence(change: dict[str, Any]) -> None:
    target = _project({**SOURCE["config"], **change})

    assert projects_equivalent(SOURCE, target) is False
    assert policy_fingerprint(SOURCE) != policy_fingerprint(target)


def test_a_disabled_default_feature_differs_from_one_left_at_its_default() -> None:
    assert projects_equivalent(_project({"features.images": "false"}), _project()) is False


def test_a_missing_project_is_never_equivalent_and_has_no_fingerprint() -> None:
    assert projects_equivalent(None, SOURCE) is False
    assert projects_equivalent(SOURCE, None) is False
    assert policy_fingerprint(None) is None


@pytest.mark.parametrize(
    ("project", "job_id"),
    [
        (_project({MIGRATION_JOB_MARKER: "job-1"}), "job-2"),
        (_project(), "job-1"),
        (_project({MIGRATION_JOB_MARKER: ""}), ""),
        (None, "job-1"),
    ],
)
def test_created_by_job_needs_the_exact_marker(
    project: Optional[dict[str, Any]], job_id: str
) -> None:
    assert created_by_job(project, job_id) is False


def test_the_default_project_always_isolates_every_feature() -> None:
    default = _project({"features.networks": "false"}, name="default")

    assert features_enabled(default, "networks") is True


@pytest.mark.parametrize(
    ("value", "enabled"),
    [("true", True), (" TRUE ", True), ("false", False), (None, False)],
)
def test_a_named_project_isolates_a_feature_only_when_it_says_true(
    value: Optional[str], enabled: bool
) -> None:
    config = {} if value is None else {"features.networks": value}

    assert features_enabled(_project(config), "networks") is enabled


def test_a_missing_project_isolates_nothing() -> None:
    assert features_enabled(None, "images") is False
