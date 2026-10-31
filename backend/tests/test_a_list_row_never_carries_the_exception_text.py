"""A list row that lost a cluster says so in the words every other list uses.

/projects and /images report one row per cluster rather than unreachable_nodes, so they sat outside
complete_reasons and `error` carried str(exc). The list services wrap what they caught in a sentence
that repeats it, which put the cluster's address in a 200 a Tenant can read, and the Images banner
printed it under Show Details.
"""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from pylxd.exceptions import LXDAPIException

from app.core import org_scope
from app.routers import images as images_router
from app.routers import projects as projects_router
from app.services import console_service, kernel_advisory_report
from app.services.lxd.exceptions import LXDConnectionError, LXDOperationError
from app.services.lxd.host_kernels import FleetKernels

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"
ADDRESS = "10.0.0.1"
RAW = f"HTTPSConnectionPool(host='{ADDRESS}', port=8443): Max retries exceeded"


class _Response:
    def __init__(self, status: int, text: str):
        self.status_code = status
        self.content = text.encode()
        self._text = text

    def json(self):
        return {"type": "error", "error": self._text}


def _wrapped(inner: Exception, noun: str) -> LXDOperationError:
    """The exception a list service raises, built the way it builds it: inside the except."""
    try:
        try:
            raise inner
        except Exception as e:
            raise LXDOperationError(f"Could not list {noun}: {str(e)}")
    except LXDOperationError as outer:
        return outer


def _transport_failure() -> requests.exceptions.ConnectionError:
    return requests.exceptions.ConnectionError(RAW)


def _refusal() -> LXDAPIException:
    return LXDAPIException(_Response(403, "project payroll is restricted"))


class _Images:
    def __init__(self, failure: Exception):
        self.failure = failure

    async def list_images(self, host, project=None):
        raise self.failure


class _Projects:
    def __init__(self, failure: Exception):
        self.failure = failure

    def get_node_names(self):
        return ["down"]

    async def list_projects(self, host):
        raise self.failure


def _image_row(failure: Exception):
    return asyncio.run(images_router._fetch_images_for_node(_Images(failure), "down"))


def _project_row(monkeypatch, failure: Exception):
    monkeypatch.setattr(
        projects_router,
        "get_cache_manager",
        lambda: SimpleNamespace(get=lambda *_a, **_k: None, set=lambda *_a, **_k: None),
    )
    rows = asyncio.run(projects_router.list_all_projects(lxd_service=_Projects(failure), fresh=True))
    return rows[0]


@pytest.fixture
def tenant():
    token = org_scope._tenant_acl_var.set({"rules": []})
    yield
    org_scope._tenant_acl_var.reset(token)


@pytest.fixture
def shared_cluster():
    token = org_scope._visible_projects_var.set({"down": {"my-project"}})
    yield
    org_scope._visible_projects_var.reset(token)


@pytest.mark.parametrize(
    ("failure", "text"),
    [
        (_wrapped(_transport_failure(), "images"), "unreachable: connection error"),
        (LXDConnectionError(RAW), "unreachable: no connection to the cluster"),
        (RuntimeError(RAW), "error: RuntimeError"),
    ],
)
def test_an_image_row_names_the_kind_and_never_the_address(failure, text):
    assert _image_row(failure).error == text


@pytest.mark.parametrize(
    ("failure", "text"),
    [
        (_wrapped(_transport_failure(), "projects"), "unreachable: connection error"),
        (LXDConnectionError(RAW), "unreachable: no connection to the cluster"),
        (RuntimeError(RAW), "error: RuntimeError"),
    ],
)
def test_a_project_row_names_the_kind_and_never_the_address(monkeypatch, failure, text):
    row = _project_row(monkeypatch, failure)

    assert row.reachable is False
    assert row.error == text


def test_the_lxd_diagnosis_stays_for_who_may_read_it():
    row = _image_row(_wrapped(_refusal(), "images"))

    assert row.error == "forbidden: LXD answered 403: project payroll is restricted"


def test_a_tenant_learns_a_row_is_missing_and_nothing_else(monkeypatch, tenant):
    image = _image_row(LXDConnectionError(RAW))
    project = _project_row(monkeypatch, LXDConnectionError(RAW))

    assert (image.reachable, image.error) == (False, None)
    assert (project.reachable, project.error) == (False, None)


def test_a_shared_cluster_keeps_the_other_organizations_names_out_of_a_row(
    monkeypatch, shared_cluster
):
    image = _image_row(_wrapped(_refusal(), "images"))
    project = _project_row(monkeypatch, _wrapped(_refusal(), "projects"))

    assert image.error == "forbidden: the cluster refused this read"
    assert project.error == "forbidden: the cluster refused this read"


def test_the_kernel_report_bounds_its_reasons_like_every_other_list(monkeypatch, shared_cluster):
    async def fleet(*_a, **_k):
        return FleetKernels(
            unreachable_nodes=["down"],
            node_errors={"down": "forbidden: LXD answered 403: project payroll is restricted"},
        )

    monkeypatch.setattr(kernel_advisory_report, "fleet_kernels", fleet)
    monkeypatch.setattr(kernel_advisory_report, "_feed_checks", lambda: {})
    monkeypatch.setattr(kernel_advisory_report, "_display_names", lambda: {})
    monkeypatch.setattr(kernel_advisory_report, "get_cache_manager", lambda: None)

    report = asyncio.run(
        kernel_advisory_report.build_kernel_report(
            SimpleNamespace(get_node_names=lambda: ["down"]), host=None, fresh=False, wall=1.0
        )
    )

    assert report.node_errors == {"down": "forbidden: the cluster refused this read"}


@pytest.mark.parametrize("step", ["connect", "pools"])
def test_the_iso_list_does_not_repeat_what_failed(monkeypatch, step):
    def broken(*_a, **_k):
        raise RuntimeError(RAW)

    client = SimpleNamespace(api=SimpleNamespace(storage_pools=SimpleNamespace(get=broken)))
    monkeypatch.setattr(console_service, "resolve_owner", lambda *_a: "default")
    monkeypatch.setattr(console_service, "project_is_visible", lambda *_a: True)
    monkeypatch.setattr(
        console_service.ConsoleService,
        "lxd_service",
        SimpleNamespace(
            ensure_connection=broken if step == "connect" else lambda _host: True,
            get_client_with_reconnect=lambda _host: client,
        ),
    )
    service = console_service.ConsoleService.__new__(console_service.ConsoleService)

    isos, error = service.get_available_isos("down")

    assert isos == []
    assert error == "ISO discovery failed. Retry the request."


def test_no_list_row_builds_its_error_from_the_exception():
    offenders = []
    rows = 0
    for path in sorted(ROUTERS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            # A row is a ListResponse carrying `reachable`, which is the shape banners read.
            if not isinstance(node, ast.Call) or not ast.unparse(node.func).endswith("ListResponse"):
                continue
            if not any(keyword.arg == "reachable" for keyword in node.keywords):
                continue
            for keyword in node.keywords:
                if keyword.arg != "error":
                    continue
                rows += 1
                value = keyword.value
                copied = isinstance(value, ast.Attribute) and value.attr == "error"
                helped = isinstance(value, ast.Call) and ast.unparse(value.func) == "failure_text"
                if not (copied or helped):
                    offenders.append(f"{path.name}:{value.lineno}")

    assert offenders == []
    # A walk over nothing passes for the wrong reason.
    assert rows >= 5
