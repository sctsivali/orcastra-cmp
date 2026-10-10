"""An add-on rotation and uninstall decide on the locked installation row.

Two rotations, or a rotation and an uninstall, each read the same api_key_id before either wrote, so
the second carried or checked a key the first had already retired, and organizations were left on a
credential no installation claims (lock_installation).
"""

import ast
import inspect
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.core.api_key_expiry import now_utc
from app.services import extension_credential_guard, extension_install, integration_key_ownership
from app.services.integration_key_ownership import OWNERSHIP_UNKNOWN_DETAIL, KeyOwnsOrganizations
from tests.test_a_capability_grant_leaves_a_record import _FakeVault
from tests.test_an_add_on_credential_keeps_its_organizations import (  # noqa: F401 - fixtures
    ADMIN,
    NEW,
    OLD,
    addon_key,
    install,
    own,
    owners,
    rotate,
    rotation,
    teardown,
    world_db,
)

def committed_meanwhile(stale: SimpleNamespace, **changes: Any):
    """A lock_installation answering what another request committed after _load read the row."""
    async def _locked(_db: Any, record: Any) -> SimpleNamespace:
        assert record is stale
        return SimpleNamespace(**{**vars(stale), **changes})

    return _locked


# note: L1, the decision is made on the locked row

async def test_a_rotation_retires_and_carries_from_the_key_the_locked_row_names(
    rotation: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world_db()
    own(db, NEW, "addon-org")
    stale = install(OLD)
    monkeypatch.setattr(extension_install, "lock_installation",
                        committed_meanwhile(stale, api_key_id=NEW, credential_generation=2))
    vault = _FakeVault(addon_key(NEW))
    record, newest, _secret = await extension_install.rotate_credential(
        db, vault, install=stale, extension=SimpleNamespace(slug="backup-bot", name="Backup Bot"),
        organization_slug="acme", actor=ADMIN.email, grace_seconds=0,
    )
    assert rotation["locks"] == [NEW], "the old key locked was the stale copy's"
    assert newest not in (OLD, NEW) and record.api_key_id == newest
    assert owners(db) == {"addon-org": newest}
    assert stale.api_key_id == OLD, "the stale copy was written to"


async def test_the_floor_reads_the_locked_row_and_mints_nothing(
    rotation: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        extension_install, "get_settings",
        lambda: SimpleNamespace(extension_credential_min_rotate_seconds=60, extension_credential_max_grace_seconds=600,
                                extension_credential_expire_days=30),
    )
    stale = install(OLD)
    monkeypatch.setattr(extension_install, "lock_installation",
                        committed_meanwhile(stale, credential_generation=2, updated_at=now_utc()))
    with pytest.raises(HTTPException) as refused:
        await rotate(world_db(), rotation["vault"], stale)
    assert refused.value.status_code == 429
    assert rotation["vault"].writes == [], "a second rotation minted before it was refused"


async def test_an_uninstall_checks_the_keys_the_locked_row_names(
    teardown: List[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world_db()
    own(db, NEW, "addon-org")
    stale = install(OLD)
    monkeypatch.setattr(extension_install, "lock_installation",
                        committed_meanwhile(stale, api_key_id=NEW, previous_api_key_id=OLD))
    with pytest.raises(KeyOwnsOrganizations) as refused:
        await extension_install.uninstall(db, object(), install=stale, user=ADMIN)
    assert refused.value.detail["api_key_id"] == NEW
    assert teardown == [] and owners(db) == {"addon-org": NEW}


async def test_the_row_is_locked_before_the_key_and_before_anything_is_minted(
    rotation: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: List[str] = []
    real_key_lock = integration_key_ownership.serialize_key_ownership
    real_mint = extension_install.rotate_install_credential

    async def _locked(_db: Any, record: Any) -> Any:
        events.append("row")
        return record

    @asynccontextmanager
    async def _key(db: Any, key_id: str) -> AsyncIterator[None]:
        events.append("key")
        async with real_key_lock(db, key_id):
            yield

    def _mint(*args: Any, **kwargs: Any) -> Any:
        events.append("mint")
        return real_mint(*args, **kwargs)

    monkeypatch.setattr(extension_install, "lock_installation", _locked)
    monkeypatch.setattr(extension_install, "serialize_key_ownership", _key)
    monkeypatch.setattr(extension_install, "rotate_install_credential", _mint)
    db = world_db()
    own(db, OLD, "addon-org")
    await rotate(db, rotation["vault"], install())
    assert events == ["row", "key", "mint"]


async def test_the_lock_is_for_no_key_update_and_reads_the_row_again() -> None:
    seen: List[Any] = []

    class _DB:
        async def execute(self, statement: Any) -> Any:
            seen.append(statement)
            return SimpleNamespace(scalar_one_or_none=lambda: "locked-row")

    assert await extension_install.lock_installation(_DB(), SimpleNamespace(id=7)) == "locked-row"
    (statement,) = seen
    sql = " ".join(str(statement.compile(dialect=postgresql.dialect())).split())
    assert "FROM extension_installations" in sql and sql.endswith("FOR NO KEY UPDATE")
    assert statement.get_execution_options()["populate_existing"] is True


async def test_a_row_deleted_meanwhile_answers_404() -> None:
    class _DB:
        async def execute(self, _statement: Any) -> Any:
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    with pytest.raises(HTTPException) as gone:
        await extension_install.lock_installation(_DB(), SimpleNamespace(id=7))
    assert gone.value.status_code == 404


async def test_ownership_that_cannot_be_read_rotates_nothing(
    rotation: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _down(_db: Any, _key_id: str) -> List[int]:
        raise ConnectionError("database is down: postgresql://user:pw@db/app")

    monkeypatch.setattr(extension_credential_guard, "owned_org_ids", _down)
    with pytest.raises(HTTPException) as refused:
        await rotate(world_db(), rotation["vault"], install())
    assert (refused.value.status_code, refused.value.detail) == (503, OWNERSHIP_UNKNOWN_DETAIL)
    assert rotation["vault"].writes == []


# note: ratchets, so the order and the one lock site hold

APP = Path(__file__).resolve().parents[1] / "app"


def _first_calls(function_name: str) -> Dict[str, int]:
    tree = ast.parse(inspect.getsource(extension_install))
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name)
    lines: Dict[str, int] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name:
                lines[name] = min(lines.get(name, node.lineno), node.lineno)
    return lines


def test_only_lock_installation_locks_an_installation_row() -> None:
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        text = path.read_text()
        if "with_for_update" not in text or "ExtensionInstallation" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "lock_installation":
                body = ast.get_source_segment(text, node) or ""
                if "with_for_update" in body and "ExtensionInstallation" in body:
                    offenders.append(f"{path.relative_to(APP)}:{node.name}")
    assert offenders == [], "an installation row is locked outside lock_installation, out of the lock order"


@pytest.mark.parametrize("function_name", ["rotate_credential", "uninstall"])
def test_the_row_is_locked_before_any_key_or_organization(function_name: str) -> None:
    lines = _first_calls(function_name)
    first_serialize = min(line for name, line in lines.items() if name.startswith("serialize_"))
    assert lines["lock_installation"] < first_serialize
