"""An image upload must land in a project the key was granted, and nowhere else.

`assert_cluster_permission` checks the cluster and returns the matched grant; the caller bounds
the project from its `projects` list, as every other key-authenticated surface does. Without that,
a key granted `projects: ["tenant-a"]` with `storage.upload` could import an image into `tenant-b`
on the same cluster.

`project` also reaches the LXD query string, where an unencoded value could reshape the request.
The name is charset-checked on the way in and percent-encoded on the way out, as `iso_uploads.py`
does for its volume and pool names.
"""

import ast
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers.image_uploads import _assert_project_permission

SOURCE = Path(__file__).resolve().parents[1] / "app" / "routers" / "image_uploads.py"
CLUSTER = "nuc2"


def _key(projects, permissions=("write",)):
    return {
        "id": "oak_test",
        "cluster_permissions": [
            {
                "cluster_id": CLUSTER,
                "permissions": list(permissions),
                **({"projects": projects} if projects is not None else {}),
            }
        ],
    }


def test_a_granted_project_is_allowed():
    _assert_project_permission(_key(["tenant-a"]), CLUSTER, "tenant-a", "write")


def test_a_project_outside_the_grant_is_refused():
    with pytest.raises(HTTPException) as caught:
        _assert_project_permission(_key(["tenant-a"]), CLUSTER, "tenant-b", "write")
    assert caught.value.status_code == 403
    assert "tenant-b" in caught.value.detail


def test_the_wildcard_grant_reaches_every_project():
    _assert_project_permission(_key(["*"]), CLUSTER, "anything", "write")


def test_an_absent_projects_field_means_default_only():
    """Absent means `default` only, as at every other enforcement site."""
    _assert_project_permission(_key(None), CLUSTER, "default", "write")
    with pytest.raises(HTTPException) as caught:
        _assert_project_permission(_key(None), CLUSTER, "tenant-a", "write")
    assert caught.value.status_code == 403


def test_an_empty_project_list_denies_everything():
    with pytest.raises(HTTPException):
        _assert_project_permission(_key([]), CLUSTER, "default", "write")


def test_an_omitted_project_is_read_as_default():
    _assert_project_permission(_key(["default"]), CLUSTER, "", "write")
    with pytest.raises(HTTPException):
        _assert_project_permission(_key(["tenant-a"]), CLUSTER, "", "write")


REQUEST_SHAPING = [
    ("a query separator", "tenant-a&target=other"),
    ("a path separator", "tenant-a/../tenant-b"),
    ("a fragment", "tenant-a#x"),
    ("a space", "tenant a"),
    ("a trailing newline", "tenant-a\n"),
]


@pytest.mark.parametrize("label,project", REQUEST_SHAPING, ids=[c[0] for c in REQUEST_SHAPING])
def test_a_project_name_that_could_reshape_the_request_is_refused(label, project):
    with pytest.raises(HTTPException) as caught:
        _assert_project_permission(_key(["*"]), CLUSTER, project, "write")
    assert caught.value.status_code == 422


def test_the_cluster_check_still_runs_first():
    with pytest.raises(HTTPException) as caught:
        _assert_project_permission(_key(["*"]), "some-other-cluster", "default", "write")
    assert caught.value.status_code == 403
    assert "cluster" in caught.value.detail


def test_the_permission_level_is_still_enforced():
    with pytest.raises(HTTPException) as caught:
        _assert_project_permission(_key(["*"], permissions=["read"]), CLUSTER, "default", "write")
    assert caught.value.status_code == 403


def test_every_handler_that_authorises_checks_the_project_too():
    """Structural: a handler that calls the cluster-only check leaves the project unbounded."""
    tree = ast.parse(SOURCE.read_text())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called = {
            child.func.id
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        if "assert_cluster_permission" in called and "_assert_project_permission" != node.name:
            offenders.append(node.name)
    assert offenders == [], (
        f"{offenders} authorise a cluster without bounding the project. Use "
        "_assert_project_permission, which does both."
    )


def test_the_project_is_encoded_before_it_reaches_the_cluster():
    source = SOURCE.read_text()
    assert 'f"?project={project}"' not in source
    assert source.count("quote(project, safe='')") == 2
