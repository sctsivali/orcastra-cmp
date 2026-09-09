"""The project-scoped reader must name the project a record really lives in.

Six findings in the EcoBytes audit came from one fact: the pooled pylxd client is bound to
LXD's `default` project, so every read answered about one project and the answer was presented
as the whole cluster. These tests pin the reader that replaces it, including the two ways it
can quietly go wrong: asking a project-bound client for every project (LXD 400s the cluster),
and walking projects that inherit `default`'s records so the same row comes back once per
project.
"""

import pytest

from app.services.lxd.project_scope import (
    DEFAULT_PROJECT,
    MAX_PROJECTS_PER_CLUSTER,
    effective_project,
    list_across_projects,
    supports_all_projects,
)


class FakeResponse:
    def __init__(self, metadata):
        self._metadata = metadata

    def json(self):
        return {"metadata": self._metadata}


class FakeNode:
    """One collection endpoint. Records the params it was asked with."""

    def __init__(self, client, collection):
        self._client = client
        self._collection = collection

    def get(self, params=None, timeout=None):
        params = params or {}
        self._client.calls.append((self._collection, dict(params)))
        if self._collection == "projects":
            if params.get("recursion") == 1:
                return FakeResponse(
                    [{"name": n, "config": c} for n, c in self._client.projects.items()]
                )
            return FakeResponse(list(self._client.projects))
        if params.get("all-projects") == "true":
            if self._client.all_projects_rows is None:
                raise AssertionError("daemon does not support all-projects")
            return FakeResponse(self._client.all_projects_rows)
        project = params.get("project", DEFAULT_PROJECT)
        if project in self._client.unreadable:
            raise RuntimeError(f"no such project {project}")
        return FakeResponse(list(self._client.per_project.get(project, [])))


class FakeApi:
    def __init__(self, client):
        self._client = client

    def __getitem__(self, collection):
        return FakeNode(self._client, collection)

    @property
    def projects(self):
        return FakeNode(self._client, "projects")


class FakeClient:
    """A pooled client (project is None) unless a test binds one."""

    def __init__(
        self,
        extensions=("networks_all_projects", "profiles_all_projects"),
        projects=None,
        per_project=None,
        all_projects_rows=None,
        project=None,
        unreadable=(),
    ):
        self.host_info = {"api_extensions": list(extensions)}
        self.projects = projects or {DEFAULT_PROJECT: {"features.networks": "true"}}
        self.per_project = per_project or {}
        self.all_projects_rows = all_projects_rows
        self.project = project
        self.unreadable = set(unreadable)
        self.calls = []
        self.api = FakeApi(self)


def test_the_extension_list_decides_whether_one_request_is_enough():
    assert supports_all_projects(FakeClient(), "networks") is True
    assert supports_all_projects(FakeClient(extensions=()), "networks") is False


def test_a_daemon_that_reports_no_extensions_at_all_is_not_a_crash():
    """A stub or an unusual daemon presents host_info with no api_extensions key."""
    client = FakeClient()
    client.host_info = {}
    assert supports_all_projects(client, "networks") is False
    client.host_info = None
    assert supports_all_projects(client, "networks") is False


@pytest.mark.parametrize(
    "flag,expected",
    [
        ("true", "tenant-a"),
        ("false", DEFAULT_PROJECT),
        (None, DEFAULT_PROJECT),
    ],
)
def test_a_project_owns_its_networks_only_when_the_feature_says_so(flag, expected):
    """Unset means false. LXD documents false as the default for every features.* option,
    and projects on this fleet omit the key rather than setting it."""
    config = {} if flag is None else {"features.networks": flag}
    assert effective_project("networks", "tenant-a", {"tenant-a": config}) == expected


def test_acls_follow_the_networks_feature_because_lxd_gives_them_no_key_of_their_own():
    configs = {"tenant-a": {"features.networks": "true", "features.profiles": "false"}}
    assert effective_project("network-acls", "tenant-a", configs) == "tenant-a"
    assert effective_project("profiles", "tenant-a", configs) == DEFAULT_PROJECT


def test_an_unknown_project_is_asked_about_as_named_rather_than_guessed():
    """A project we hold no config for must not be silently rewritten to default; LXD's 404
    is the answer the caller needs to see."""
    assert effective_project("networks", "ghost", {}) == "ghost"


def test_one_request_answers_when_the_daemon_supports_it():
    client = FakeClient(
        all_projects_rows=[
            {"name": "lxdbr0", "project": DEFAULT_PROJECT},
            {"name": "ovn-uat-hermes", "project": "UAT-Hermes"},
        ]
    )
    rows, complete = list_across_projects(client, "networks")
    assert complete is True
    assert {r["name"] for r in rows} == {"lxdbr0", "ovn-uat-hermes"}
    assert [c for c in client.calls if c[0] == "networks"] == [
        ("networks", {"recursion": 1, "all-projects": "true"})
    ]


def test_a_project_bound_client_is_refused_before_it_reaches_the_daemon():
    """pylxd injects params['project'] on every verb when the client carries one, and LXD
    refuses project together with all-projects. Sending it would 400 the whole cluster."""
    client = FakeClient(project=DEFAULT_PROJECT)
    with pytest.raises(ValueError, match="pooled client"):
        list_across_projects(client, "networks")
    assert client.calls == []


def test_an_inherited_project_does_not_return_the_same_record_twice():
    """47 projects with features.networks unset all see default's networks. Walking them
    naively returns one bridge 47 times and makes the total a lie."""
    projects = {DEFAULT_PROJECT: {"features.networks": "true"}}
    for i in range(47):
        projects[f"inherit-{i}"] = {"features.networks": "false"}
    client = FakeClient(
        extensions=(),
        projects=projects,
        per_project={DEFAULT_PROJECT: [{"name": "lxdbr0"}]},
    )
    rows, complete = list_across_projects(client, "networks")
    assert complete is True
    assert [r["name"] for r in rows] == ["lxdbr0"]
    assert all(r["project"] == DEFAULT_PROJECT for r in rows)
    # default read once, not 48 times
    reads = [c for c in client.calls if c[0] == "networks"]
    assert len(reads) == 1, reads


def test_a_project_that_owns_its_networks_is_read_on_its_own():
    projects = {
        DEFAULT_PROJECT: {"features.networks": "true"},
        "UAT-Hermes": {"features.networks": "true"},
        "shared": {"features.networks": "false"},
    }
    client = FakeClient(
        extensions=(),
        projects=projects,
        per_project={
            DEFAULT_PROJECT: [{"name": "br-uplink"}],
            "UAT-Hermes": [{"name": "ovn-uat-hermes"}],
        },
    )
    rows, complete = list_across_projects(client, "networks")
    assert complete is True
    assert {(r["name"], r["project"]) for r in rows} == {
        ("br-uplink", DEFAULT_PROJECT),
        ("ovn-uat-hermes", "UAT-Hermes"),
    }


def test_a_project_deleted_mid_walk_makes_the_read_partial_not_empty():
    projects = {
        DEFAULT_PROJECT: {"features.networks": "true"},
        "vanishing": {"features.networks": "true"},
    }
    client = FakeClient(
        extensions=(),
        projects=projects,
        per_project={DEFAULT_PROJECT: [{"name": "lxdbr0"}]},
        unreadable=("vanishing",),
    )
    rows, complete = list_across_projects(client, "networks")
    assert [r["name"] for r in rows] == ["lxdbr0"]
    assert complete is False


def test_a_labelless_all_projects_answer_is_re_read_rather_than_attributed_by_guesswork():
    """A daemon that accepts the parameter but answers without a project field cannot be
    used: every row would have to be attributed by guessing."""
    client = FakeClient(
        projects={
            DEFAULT_PROJECT: {"features.networks": "true"},
            "tenant-a": {"features.networks": "true"},
        },
        all_projects_rows=[{"name": "lxdbr0"}],
        per_project={
            DEFAULT_PROJECT: [{"name": "lxdbr0"}],
            "tenant-a": [{"name": "tenant-br0"}],
        },
    )
    rows, complete = list_across_projects(client, "networks")
    assert complete is True
    assert {(r["name"], r["project"]) for r in rows} == {
        ("lxdbr0", DEFAULT_PROJECT),
        ("tenant-br0", "tenant-a"),
    }


def test_a_cluster_with_more_projects_than_the_cap_is_reported_partial():
    projects = {f"p{i}": {"features.networks": "true"} for i in range(MAX_PROJECTS_PER_CLUSTER + 5)}
    client = FakeClient(extensions=(), projects=projects, per_project={})
    _rows, complete = list_across_projects(client, "networks")
    assert complete is False
