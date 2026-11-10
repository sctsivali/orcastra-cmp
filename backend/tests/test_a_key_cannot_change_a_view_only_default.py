"""A key a partner created cannot change, or reach inside a guest in, a view-only `default`.

`default` on a cluster another party also holds is view only for a Partner in the browser
(test_default_is_view_only_where_another_party_holds_the_cluster.py). That rule reads the request's
own org scope, which no key route carries, so for a key it is judged from the key's creator: the
party from the database, the role from Authentik. It covers writes, guest sessions and guest files
through the structured proxy, MCP and the raw proxy. Reads stay open, an Admin's key is exempt, and
an unanswered identity provider is a retryable 503, never a verdict.
"""

import ast
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.services import key_default_view_only as kdv
from app.services.key_default_view_only import CreatorRoleUnavailable, refuse_key_default_operation
from tests._limited_write_fakes import ProjectSession, mcp_call, proxy_call, raw_write
from tests._raw_proxy_fakes import CLUSTER_ID, RecordingSession, call_raw_proxy, key

PARTNER_KEY = {**key(["*"]), "created_by": "partner@example.test"}


@pytest.fixture
def view_only(monkeypatch):
    """The cluster is held by another party for every creator; the creator is not an Admin."""
    state: Dict[str, Any] = {"held": True, "admin": False, "judged": [], "asked": []}

    async def judge(api_key, cluster_id, _db):
        creator = await kdv._creator(api_key, _db) if api_key.get("extension_install_id") else str(api_key.get("created_by") or "")
        state["judged"].append((creator, cluster_id))
        return creator, state["held"]

    async def is_admin(email, *, roles=None):
        state["asked"].append(email)
        return state["admin"]

    monkeypatch.setattr(kdv, "_judge", judge)
    monkeypatch.setattr(kdv, "creator_is_admin", is_admin)
    return state


def _refusal(result) -> Optional[HTTPException]:
    return result if isinstance(result, HTTPException) else None


# ── the rule itself ─────────────────────────────────────────────────────────────────────────────

async def test_a_partner_key_is_refused_in_a_held_default(view_only) -> None:
    with pytest.raises(HTTPException) as refused:
        await refuse_key_default_operation(PARTNER_KEY, CLUSTER_ID, ["default"])
    assert refused.value.status_code == 403
    assert "view only for keys a partner created" in refused.value.detail
    assert view_only["judged"] == [("partner@example.test", CLUSTER_ID)]


async def test_an_admin_created_key_still_changes_default(view_only) -> None:
    view_only["admin"] = True
    await refuse_key_default_operation(PARTNER_KEY, CLUSTER_ID, ["default"])


async def test_an_unanswered_idp_is_a_retryable_503_not_a_verdict(view_only) -> None:
    view_only["admin"] = None
    with pytest.raises(HTTPException) as refused:
        await refuse_key_default_operation(PARTNER_KEY, CLUSTER_ID, ["default"])
    assert refused.value.status_code == 503
    assert refused.value.headers["Retry-After"] == "30"
    assert isinstance(refused.value.__cause__, CreatorRoleUnavailable)
    assert refused.value.detail["error"] == "identity_provider_unavailable"


async def test_a_cluster_no_other_party_holds_never_asks_the_idp(view_only) -> None:
    view_only["held"] = False
    await refuse_key_default_operation(PARTNER_KEY, CLUSTER_ID, ["default"])
    assert view_only["asked"] == []


async def test_a_project_other_than_default_is_not_judged(view_only) -> None:
    await refuse_key_default_operation(PARTNER_KEY, CLUSTER_ID, ["tenant-a"])
    await refuse_key_default_operation(PARTNER_KEY, CLUSTER_ID, [])
    assert view_only["judged"] == []


async def test_an_unnamed_project_is_default() -> None:
    assert kdv.touches_default([None]) and kdv.touches_default([""]) and kdv.touches_default([" default "])
    assert not kdv.touches_default(["tenant-a", "*"])


async def test_an_unreadable_party_is_a_503(monkeypatch) -> None:
    async def broken(_api_key, _cluster_id, _db):
        raise RuntimeError("database gone")

    monkeypatch.setattr(kdv, "_judge", broken)
    with pytest.raises(HTTPException) as refused:
        await refuse_key_default_operation(PARTNER_KEY, CLUSTER_ID, ["default"], db=object())
    assert refused.value.status_code == 503


async def test_an_add_on_is_judged_by_its_installer_not_its_rotator(monkeypatch) -> None:
    from app.services import extension_scope

    async def snapshot(_db, key_id):
        assert key_id == "oak_addon"
        return {"installed_by": "installer@example.test"}

    monkeypatch.setattr(extension_scope, "load_install_snapshot", snapshot)
    addon = {"id": "oak_addon", "created_by": "rotator@example.test", "extension_install_id": 7}
    assert await kdv._creator(addon, db=object()) == "installer@example.test"
    assert await kdv._creator({"id": "oak_plain", "created_by": "c@example.test"}, db=None) == "c@example.test"


async def test_the_party_is_read_through_the_shared_rule_and_cached(monkeypatch) -> None:
    from app.services import cluster_parties

    kdv.forget_key_default_verdicts()
    calls: List[Any] = []

    async def shared(_db, email, cluster_ids):
        calls.append((email, set(cluster_ids)))
        return {CLUSTER_ID}

    monkeypatch.setattr(cluster_parties, "default_view_only_clusters", shared)
    assert await kdv._held_by_another_party(object(), "P@Example.test", CLUSTER_ID) is True
    assert await kdv._held_by_another_party(object(), "p@example.test", CLUSTER_ID) is True
    assert calls == [("P@Example.test", {CLUSTER_ID})], "one read per creator and cluster in the window"


# ── every door ─────────────────────────────────────────────────────────────────────────────────

async def test_a_raw_write_into_default_is_refused_before_anything_is_sent(view_only) -> None:
    result, session = await raw_write("PUT", "/1.0/instances/web", "default", {"config": {}}, api_key=PARTNER_KEY)
    assert _refusal(result) is not None and result.status_code == 403
    assert not session.sent


async def test_a_raw_read_of_default_still_answers(view_only) -> None:
    result, _session = await call_raw_proxy(PARTNER_KEY, "GET", "/1.0/instances", project="default")
    assert _refusal(result) is None


async def test_a_raw_guest_file_read_in_default_is_refused(view_only) -> None:
    api_key = {**PARTNER_KEY, "capabilities": ["lxd.proxy", "instance.files"]}
    result, session = await call_raw_proxy(api_key, "GET", "/1.0/instances/web/files?path=/etc/shadow", project="default")
    assert _refusal(result) is not None and result.status_code == 403
    assert session.calls == []


async def test_a_raw_write_to_the_default_project_itself_is_refused(view_only) -> None:
    result, _session = await raw_write("PUT", "/1.0/projects/default", "tenant-a", {"config": {}}, api_key=PARTNER_KEY)
    assert _refusal(result) is not None and result.status_code == 403


async def test_a_move_into_default_named_in_the_body_is_refused(view_only) -> None:
    result, session = await raw_write("POST", "/1.0/instances/web", "tenant-a", {"name": "web", "project": "default"},
                                      api_key=PARTNER_KEY)
    assert _refusal(result) is not None and result.status_code == 403
    assert not session.sent


async def test_a_raw_write_in_the_keys_own_project_is_untouched(view_only) -> None:
    await raw_write("PUT", "/1.0/instances/web", "tenant-a", {"config": {}}, api_key=PARTNER_KEY)
    assert view_only["judged"] == [], "a write in the key's own project is never judged by this rule"


async def test_a_structured_write_into_default_is_refused(view_only) -> None:
    result, client = await proxy_call("start_instance", "default", {"name": "web"}, api_key=PARTNER_KEY)
    assert _refusal(result) is not None and result.status_code == 403
    assert client.sent == []


async def test_a_structured_read_of_default_still_answers(view_only) -> None:
    result, _client = await proxy_call("list_projects", "default", {}, api_key=PARTNER_KEY)
    assert _refusal(result) is None
    assert view_only["judged"] == []


async def test_a_structured_guest_read_in_default_is_refused(view_only) -> None:
    api_key = {**PARTNER_KEY, "capabilities": ["lxd.proxy", "instance.files"]}
    result, _client = await proxy_call("get_file", "default", {"name": "web", "path": "/etc/shadow"}, api_key=api_key)
    assert _refusal(result) is not None and result.status_code == 403


async def test_an_mcp_write_into_default_is_refused(view_only, monkeypatch) -> None:
    from app.mcp import tools_write
    from tests._limited_write_fakes import FakeClient

    client = FakeClient(ProjectSession())
    result = await mcp_call(tools_write, "lxd_start_instance", client, PARTNER_KEY, monkeypatch,
                            cluster_id=CLUSTER_ID, project="default", name="web")
    assert result.is_error and "view only for keys a partner created" in result.content[0].text, result
    assert result.content[0].text.startswith("[forbidden]")
    assert client.sent == []


# Every key door that writes, or reads inside a guest, reaches the gate.
_DOORS = {
    "routers/integrations.py": {"raw_proxy", "proxy_operation", "provision_tenant_access", "sync_policies_from_external"},
    "routers/iso_uploads.py": {"create_upload", "finalize_upload"},
    "routers/image_uploads.py": {"create_upload", "finalize_upload"},
}


def test_every_key_write_door_reaches_the_default_gate() -> None:
    app_dir = Path(kdv.__file__).resolve().parents[1]
    missing = []
    for relative, handlers in _DOORS.items():
        tree = ast.parse((app_dir / relative).read_text())
        functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
        for name in handlers:
            called = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
                      for c in ast.walk(functions[name]) if isinstance(c, ast.Call)}
            if "refuse_key_default_operation" not in called:
                missing.append(f"{relative}::{name}")
    assert not missing, missing


def test_a_browser_tenant_rule_naming_a_view_only_default_is_refused() -> None:
    from types import SimpleNamespace

    from app.routers.tenant_access import _refuse_a_project_the_caller_cannot_see
    from tests.test_default_is_view_only_where_another_party_holds_the_cluster import HELD, WHOLE, _partner

    with _partner("POST", "/api/v1/tenant-access/policies"):
        for project in ("default", "*", ""):
            with pytest.raises(HTTPException) as refused:
                _refuse_a_project_the_caller_cannot_see(SimpleNamespace(cluster_id=HELD, project_name=project))
            assert refused.value.status_code == 403
        _refuse_a_project_the_caller_cannot_see(SimpleNamespace(cluster_id=HELD, project_name="own"))
        _refuse_a_project_the_caller_cannot_see(SimpleNamespace(cluster_id=WHOLE, project_name="default"))


# ── where a write lands ─────────────────────────────────────────────────────────────────────────

async def test_a_structured_copy_out_of_default_is_refused(view_only) -> None:
    params = {"name": "stolen", "source": {"type": "copy", "source": "their-vm", "project": "default"}}
    result, client = await proxy_call("create_instance", "tenant-a", params, api_key=PARTNER_KEY)
    assert _refusal(result) is not None and result.status_code == 403
    assert client.created == []


async def test_a_raw_write_into_a_project_that_keeps_its_profiles_in_default_is_refused(view_only, monkeypatch) -> None:
    from app.routers import integrations

    monkeypatch.setattr(integrations, "resolve_owner", lambda _client, collection, project: "default")
    result, session = await raw_write("PUT", "/1.0/profiles/web", "tenant-a", {"config": {}}, api_key=PARTNER_KEY)
    assert _refusal(result) is not None and result.status_code == 403
    assert not session.sent


async def test_a_raw_write_into_a_project_with_its_own_store_is_not_this_rules_business(view_only, monkeypatch) -> None:
    from app.routers import integrations

    asked = []
    monkeypatch.setattr(integrations, "resolve_owner",
                        lambda _client, collection, project: asked.append(collection) or project)
    result, _session = await raw_write("PUT", "/1.0/profiles/web", "tenant-a", {"config": {}}, api_key=PARTNER_KEY)
    assert asked == ["profiles"]
    assert not (_refusal(result) is not None and "view only for keys" in str(result.detail))


async def test_cancelling_an_operation_of_default_is_refused(view_only, monkeypatch) -> None:
    from app.routers import integrations

    monkeypatch.setattr(integrations, "operation_is_visible",
                        lambda _session, _endpoint, _op, projects, _timeout: projects == frozenset({"default"}))
    result, session = await raw_write("DELETE", "/1.0/operations/0b9e5b1e-1111-4c7e-9a4f-0f5d2a7f3c21",
                                      "tenant-a", None, api_key=PARTNER_KEY)
    assert _refusal(result) is not None and result.status_code == 403
    assert not session.sent


async def test_where_a_write_lands_is_asked_only_on_a_held_cluster(view_only) -> None:
    asked = []

    async def lands_in():
        asked.append(1)
        return ["default"]

    view_only["held"] = False
    await refuse_key_default_operation(PARTNER_KEY, CLUSTER_ID, ["tenant-a"], lands_in=lands_in)
    assert asked == []
    view_only["held"] = True
    with pytest.raises(HTTPException) as refused:
        await refuse_key_default_operation(PARTNER_KEY, CLUSTER_ID, ["tenant-a"], lands_in=lands_in)
    assert refused.value.status_code == 403 and asked == [1]


async def test_a_landing_in_the_named_project_passes_and_an_unknown_one_is_a_503(view_only) -> None:
    async def own():
        return ["tenant-a"]

    async def broken():
        raise RuntimeError("cluster went away")

    await refuse_key_default_operation(PARTNER_KEY, CLUSTER_ID, ["tenant-a"], lands_in=own)
    with pytest.raises(HTTPException) as refused:
        await refuse_key_default_operation(PARTNER_KEY, CLUSTER_ID, ["tenant-a"], lands_in=broken)
    assert refused.value.status_code == 503


def test_image_uploads_ask_where_the_image_lands() -> None:
    source = (Path(kdv.__file__).resolve().parents[1] / "routers" / "image_uploads.py").read_text()
    functions = {n.name: n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef)}
    for name in ("create_upload", "finalize_upload"):
        gates = [c for c in ast.walk(functions[name]) if isinstance(c, ast.Call)
                 and getattr(c.func, "id", None) == "refuse_key_default_operation"]
        assert gates and any(k.arg == "lands_in" for k in gates[0].keywords), name
