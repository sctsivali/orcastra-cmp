"""An image upload must land in a project the key was granted, and nowhere else.

`image_uploads.py` checked the cluster and stopped. `assert_cluster_permission` returns the
matched grant so a caller can read its `projects` list, and its docstring says exactly that, but
this router discarded the return value. So a key granted `projects: ["tenant-a"]` with
`storage.upload` could open a session for `tenant-b` on the same cluster and import an image
into it. Every other key-authenticated surface enforces that boundary: the proxy, the raw proxy,
the operation reads and the instance reads all do. `iso_uploads.py` refuses a non-default project
outright. This router was the hole.

A second defect sat behind the same field. `project` reached the LXD query string through an
f-string with no encoding, so the value did not merely name a project, it could reshape the
request. The name is now charset-checked on the way in and percent-encoded on the way out, the
same two-sided treatment `iso_uploads.py` gives its volume and pool names.
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
    """Same reading as every other enforcement site: absent is narrow, not unrestricted."""
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
    """Structural, because the defect was a call site that checked one half and moved on.

    A new handler that reaches for the cluster-only check would reopen it silently.
    """
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
