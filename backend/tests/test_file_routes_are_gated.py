"""Who may reach a guest's files, and the ratchets that keep it so as the routes grow."""

import ast
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import file_ticket, org_scope
from app.core.config import get_settings
from app.core.error_sanitizer import public_server_detail
from app.core.instance_access import verify_file_access
from app.core.ws_ticket import TICKET_TTL_SECONDS
from app.core.ws_ticket_store import get_ws_ticket_store
from app.models.instance_file_schemas import PathBody
from app.routers import instance_file_transfer as transfer
from app.routers import instance_files
from app.services.instance_files import session as file_session

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"
HOST = "shared-cluster"
INSTANCE = "vm1"
OWN_PROJECT = "tenant-project"
FOREIGN_PROJECT = "other-org"


def _rule(permissions, project=OWN_PROJECT, instance=INSTANCE):
    return SimpleNamespace(
        cluster_id=HOST, project_name=project, instance_pattern=instance, permissions=permissions
    )


@pytest.fixture
def partner_scope():
    allowed = org_scope._allowed_clusters_var.set({HOST})
    projects = org_scope._visible_projects_var.set({HOST: {"default", OWN_PROJECT}})
    acl = org_scope._tenant_acl_var.set(None)
    try:
        yield
    finally:
        org_scope._tenant_acl_var.reset(acl)
        org_scope._visible_projects_var.reset(projects)
        org_scope._allowed_clusters_var.reset(allowed)


def _tenant_with(monkeypatch, permissions):
    def check(cluster_id, project_name="default", instance_name=None, required_permission="view"):
        return required_permission in permissions

    monkeypatch.setattr("app.core.instance_access.verify_tenant_permission", _raising(check))


def _raising(check):
    def verify(cluster_id, project_name="default", instance_name=None, required_permission="view"):
        if not check(cluster_id, project_name, instance_name, required_permission):
            raise HTTPException(status_code=403, detail="no permission")

    return verify


def test_a_tenant_without_the_grant_is_refused(partner_scope, monkeypatch):
    _tenant_with(monkeypatch, {"view", "console"})
    with pytest.raises(HTTPException) as refused:
        verify_file_access(HOST, OWN_PROJECT, INSTANCE)
    assert refused.value.status_code == 403


def test_a_tenant_with_the_grant_gets_the_bound_project(partner_scope, monkeypatch):
    _tenant_with(monkeypatch, {"view", "file_transfer"})
    assert verify_file_access(HOST, OWN_PROJECT, INSTANCE) == OWN_PROJECT


def test_switching_console_enforcement_off_does_not_open_files(partner_scope, monkeypatch):
    # That flag relaxes the console-grant rollout; honouring it here would give every viewer root.
    monkeypatch.setattr(get_settings(), "tenant_perm_enforcement_enabled", False, raising=False)
    _tenant_with(monkeypatch, {"view"})
    with pytest.raises(HTTPException):
        verify_file_access(HOST, OWN_PROJECT, INSTANCE)


def test_a_foreign_project_is_refused(partner_scope):
    with pytest.raises(HTTPException) as refused:
        verify_file_access(HOST, FOREIGN_PROJECT, INSTANCE)
    assert refused.value.status_code in (403, 404)


def test_a_foreign_cluster_is_refused(partner_scope):
    with pytest.raises(HTTPException):
        verify_file_access("not-mine", OWN_PROJECT, INSTANCE)


async def test_an_admin_without_a_grant_never_reaches_the_guest(monkeypatch):
    opened = []

    async def refuse(user, host):
        raise HTTPException(status_code=403, detail={"error": "break_glass_required"})

    def open_guest(*args, **kwargs):
        opened.append(args)
        raise AssertionError("the guest must not be opened")

    monkeypatch.setattr(file_session, "sensitive_gate", refuse)
    monkeypatch.setattr(file_session, "open_guest", open_guest)
    with pytest.raises(HTTPException) as refused:
        async with file_session.guest_session(SimpleNamespace(user_id="admin"), HOST, "default", INSTANCE):
            pass
    assert refused.value.detail["error"] == "break_glass_required"
    assert opened == []


async def test_a_fenced_instance_answers_with_a_code_the_interface_can_read(monkeypatch):
    from app.services.migration_fence import MigrationFenceConflict

    async def fenced(**kwargs):
        raise MigrationFenceConflict("Migration Center owns this instance")

    async def no_grant(user, host):
        return None

    monkeypatch.setattr(file_session, "sensitive_gate", no_grant)
    monkeypatch.setattr(file_session, "assert_instance_not_migration_fenced", fenced)
    with pytest.raises(HTTPException) as refused:
        async with file_session.guest_session(None, HOST, "default", INSTANCE):
            pass
    assert refused.value.status_code == 409
    assert refused.value.detail["error"] == "migration_fenced"


def test_a_disabled_deployment_answers_503(monkeypatch):
    monkeypatch.setattr(get_settings(), "instance_files_enabled", False, raising=False)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(instance_files.require_files_enabled())
    assert refused.value.status_code == 503
    assert refused.value.detail["error"] == "feature_disabled"


def _handlers(filename):
    tree = ast.parse((ROUTERS / filename).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            "router." in ast.unparse(decorator) for decorator in node.decorator_list
        ):
            yield node


@pytest.mark.parametrize("filename", ["instance_files.py", "instance_file_transfer.py"])
def test_every_handler_calls_the_file_gate(filename):
    missing = [node.name for node in _handlers(filename) if "verify_file_access" not in ast.unparse(node)]
    assert missing == []


@pytest.mark.parametrize("module", [instance_files, transfer])
def test_the_routers_carry_org_scope_and_a_role(module):
    calls = [getattr(dep.dependency, "__qualname__", "") for dep in module.router.dependencies]
    assert "org_scope_dependency" in calls
    assert any(name.startswith("require_roles") for name in calls)
    assert "release_request_db" in calls
    assert "require_files_enabled" in calls


def test_every_change_refuses_in_read_only_mode():
    reads = {"list_directory", "stat_path", "summarize", "read_text", "preview_image", "create_download", "redeem_download"}
    for filename in ("instance_files.py", "instance_file_transfer.py"):
        for node in _handlers(filename):
            if node.name in reads:
                continue
            decorator = "".join(ast.unparse(d) for d in node.decorator_list)
            assert "check_read_only_mode" in decorator, node.name


def test_no_cluster_lock_is_held_across_a_guest_round_trip():
    package = Path(__file__).resolve().parents[1] / "app" / "services" / "instance_files"
    sources = [(ROUTERS / "instance_files.py"), (ROUTERS / "instance_file_transfer.py"), *package.glob("*.py")]
    for path in sources:
        tree = ast.parse(path.read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "guard_instance_mutation" not in names, path.name
        assert "guard_migration_mutations" not in names, path.name


def test_no_route_depends_on_content_length():
    """The proxy streams request bodies and drops Content-Length, so a browser never sends one.

    A route that required it would answer every browser chunk with 422 while direct API calls
    pass.
    """
    for filename in ("instance_files.py", "instance_file_transfer.py"):
        assert 'alias="Content-Length"' not in (ROUTERS / filename).read_text()


def test_paths_never_travel_in_a_query():
    for filename in ("instance_files.py", "instance_file_transfer.py"):
        for node in _handlers(filename):
            args = [a.arg for a in list(node.args.args) + list(node.args.kwonlyargs)]
            assert not {"path", "file", "dir", "filename", "file_path", "dest_path"} & set(args), node.name


def test_the_ticket_route_redeems_through_its_own_gate():
    source = next(ast.unparse(n) for n in _handlers("instance_file_transfer.py") if n.name == "redeem_download")
    order = [
        source.index(needle)
        for needle in (
            "file_ticket.redeem",
            "session_has_role",
            "_populate_request_state",
            "set_org_scope_for_ws",
            "verify_file_access",
            "sensitive_gate",
            "fence_gate",
        )
    ]
    assert order == sorted(order)
    assert "interactive=True" in source


async def test_a_ticket_opens_one_download_and_only_its_own_door():
    user = SimpleNamespace(
        user_id="u1", email="u1@example", name="U", groups=["role_tenant"], roles=[], raw_claims={}, session_exp=None
    )
    ticket = await file_ticket.mint(
        user, host=HOST, project="default", instance=INSTANCE, paths=["/etc/hosts"],
        archive=False, name="hosts", size=10, op_id="op1",
    )
    assert await get_ws_ticket_store().redeem(ticket) is None
    download = await file_ticket.redeem(ticket)
    assert download.paths == ("/etc/hosts",)
    assert (download.host, download.instance) == (HOST, INSTANCE)
    assert await file_ticket.redeem(ticket) is None


async def test_a_websocket_ticket_is_no_download_ticket():
    ws_ticket = await get_ws_ticket_store().mint("u1", '{"kind":"terminal"}')
    assert await file_ticket.redeem(ws_ticket) is None


async def test_an_old_ticket_is_refused(monkeypatch):
    user = SimpleNamespace(
        user_id="u1", email="u1@example", name="U", groups=[], roles=[], raw_claims={}, session_exp=None
    )
    ticket = await file_ticket.mint(
        user, host=HOST, project="default", instance=INSTANCE, paths=["/f"],
        archive=False, name="f", size=1, op_id="op1",
    )
    later = time.time() + TICKET_TTL_SECONDS + 60
    monkeypatch.setattr(file_ticket.time, "time", lambda: later)
    assert await file_ticket.redeem(ticket) is None


@pytest.mark.parametrize(
    "status,code",
    [
        (502, "guest_protocol_error"),
        (502, "read_only_fs"),
        (503, "agent_unavailable"),
        (503, "guest_disconnected"),
        (504, "guest_timeout"),
        (507, "no_space"),
    ],
)
def test_a_guest_failure_keeps_its_code_through_the_sanitizer(status, code):
    detail = public_server_detail(status, {"error": code, "message": "words"}, "ref")
    assert detail == {"error": code, "message": "words"}


def test_a_path_body_rejects_nothing_a_guest_may_hold():
    assert PathBody(path="/etc/shadow").path == "/etc/shadow"
    assert PathBody(path="/srv/.env").path == "/srv/.env"


async def test_a_missing_instance_is_an_answer_not_a_fault(monkeypatch):
    from pylxd.exceptions import NotFound

    from app.services import lxd_ws
    from app.services.instance_files import tunnel
    from app.services.instance_files.errors import GuestFileError

    class _Instances:
        def __getitem__(self, name):
            return self

        def get(self, **kwargs):
            raise NotFound(SimpleNamespace(json=lambda: {"error": "Instance not found"}, status_code=404, url="u", text=""))

    client = SimpleNamespace(api=SimpleNamespace(instances=_Instances()))
    monkeypatch.setattr(lxd_ws, "lxd_client", lambda host: client)

    async def direct(fn, *args, label, host, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(lxd_ws, "session_io", direct)
    with pytest.raises(GuestFileError) as refused:
        await tunnel._running_record(HOST, "default", "gone")
    assert refused.value.code == "instance_not_found"
    assert refused.value.status == 404


async def test_a_chunk_for_a_fenced_instance_is_refused_before_its_body_is_read(monkeypatch):
    from app.services.migration_fence import MigrationFenceConflict

    class _Unread:
        def stream(self):
            pytest.fail("the body was read after the fence refused")

    async def fenced(**kwargs):
        raise MigrationFenceConflict("Migration Center owns this instance")

    async def no_grant(user, host):
        return None

    monkeypatch.setattr(transfer, "verify_file_access", lambda host, project, name: project)
    monkeypatch.setattr(transfer, "_session_of", lambda *args: SimpleNamespace())
    monkeypatch.setattr(file_session, "sensitive_gate", no_grant)
    monkeypatch.setattr(file_session, "assert_instance_not_migration_fenced", fenced)
    with pytest.raises(HTTPException) as refused:
        await transfer.write_upload_chunk(
            request=_Unread(), host=HOST, project="default", instance_name=INSTANCE,
            upload_token="t", upload_offset=0, upload_length=1, user=None,
        )
    assert refused.value.detail["error"] == "migration_fenced"


@pytest.mark.parametrize(
    "method,interactive,refused",
    [
        pytest.param("POST", False, True, id="a POST read is a change"),
        pytest.param("GET", True, True, id="the ticket route redeems as interactive"),
        pytest.param("GET", False, False, id="why reads are not GET: this one would pass"),
    ],
)
def test_a_cluster_past_its_paid_term_refuses_files_like_a_console(partner_scope, monkeypatch, method, interactive, refused):
    from app.core import request_context

    context = SimpleNamespace(method=method, path="/api/v1/instance-files/list")
    monkeypatch.setattr(request_context, "get_request_context", lambda: context)
    frozen = org_scope._read_only_clusters_var.set(frozenset({HOST}))
    flag = org_scope._interactive_var.set(interactive)
    try:
        if refused:
            with pytest.raises(HTTPException) as denied:
                verify_file_access(HOST, OWN_PROJECT, INSTANCE)
            assert denied.value.status_code == 403
        else:
            assert verify_file_access(HOST, OWN_PROJECT, INSTANCE) == OWN_PROJECT
    finally:
        org_scope._interactive_var.reset(flag)
        org_scope._read_only_clusters_var.reset(frozen)


@pytest.mark.parametrize(
    "server,version,supported",
    [
        ("lxd", "5.21.8", True),
        ("lxd", "5.21.6", True),
        ("lxd", "5.21.5", False),
        ("lxd", "5.0.8", True),
        ("lxd", "5.0.7", False),
        ("lxd", "4.0.12", True),
        ("lxd", "6.9", True),
        ("lxd", "6.8", False),
        ("incus", "7.3.0", True),
        ("incus", "7.2.0", False),
        # The LTS lines carry the symlink fixes as backports from 7.0.2 and 6.0.7 on
        # (test_every_feature_floor_reads_one_table.py has the commits).
        ("incus", "7.0.2", True),
        ("incus", "7.0.1", False),
        ("incus", "6.0.7", True),
        ("incus", "6.0.6", False),
        ("lxd", "not-a-version", False),
        ("", "", False),
    ],
)
def test_only_a_patched_daemon_is_reached(server, version, supported):
    from app.services.instance_files.tunnel import is_supported_server

    assert is_supported_server(server, version) is supported


async def test_a_read_under_a_grant_always_leaves_a_record(monkeypatch):
    written = []

    async def grant(user, host):
        return 77

    async def no_fence(*args):
        return None

    async def record(action, **kwargs):
        written.append((action, kwargs["details"]))

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_guest(*args, **kwargs):
        yield object()

    monkeypatch.setattr(file_session, "sensitive_gate", grant)
    monkeypatch.setattr(file_session, "fence_gate", no_fence)
    monkeypatch.setattr(file_session, "write", record)
    monkeypatch.setattr(file_session, "open_guest", fake_guest)
    async with file_session.guest_session(SimpleNamespace(user_id="admin"), HOST, "default", INSTANCE, route="stat"):
        pass
    assert written == [("file.browse.granted", {"route": "stat", "break_glass_grant_id": 77})]


def test_every_change_writes_an_audit_record():
    unaudited = {"list_directory", "stat_path", "summarize", "create_download", "redeem_download", "upload_status", "write_upload_chunk"}
    for filename in ("instance_files.py", "instance_file_transfer.py"):
        for node in _handlers(filename):
            if node.name not in unaudited:
                assert "audited(" in ast.unparse(node), node.name


def test_a_nul_in_a_path_is_refused_before_anything_is_audited():
    from pydantic import ValidationError

    from app.models.instance_file_schemas import RenameBody

    with pytest.raises(ValidationError):
        PathBody(path="/x\x00")
    with pytest.raises(ValidationError):
        RenameBody(source="/a", target="/b\x00")


def test_the_journal_never_holds_a_nul():
    from app.services.task_journal.vocabulary import bounded_json

    assert bounded_json({"path": "/x\x00y", "nested": {"a": ["\x00b"]}}) == {"path": "/xy", "nested": {"a": ["b"]}}


async def test_a_selection_cannot_become_a_ticket_that_fills_redis():
    from pydantic import ValidationError

    from app.models.instance_file_schemas import DownloadBody

    # Each private code point counts once here and takes 12 bytes as JSON.
    with pytest.raises(ValidationError):
        DownloadBody(paths=["/" + "\U0010fc80" * 8000] * 100, archive=True)
    user = SimpleNamespace(user_id="u1", email="u@x", name="U", groups=[], roles=[], raw_claims={}, session_exp=None)
    with pytest.raises(file_ticket.TicketTooLarge):
        await file_ticket.mint(
            user, host=HOST, project="default", instance=INSTANCE, paths=["/" + "\U0010fc80" * 4000] * 10,
            archive=True, name="x", size=None, op_id="op",
        )
