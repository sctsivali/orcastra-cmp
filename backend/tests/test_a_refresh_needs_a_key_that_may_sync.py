"""A refresh is refused to a key that /sync would refuse, and to an add-on credential.

refresh-organizations claims unclaimed organizations and renames owned ones, the writes /sync gates
on. A key that lost org.sync or inventory.read keeps its stored callback URL, and an add-on credential
may have stored one through /sync, so a refresh would still write organizations or send the key's hash
to that URL. Both answer 409 before the secret is read or the callback is called. A key issued before
capabilities existed holds them all.
"""

from typing import Any, Dict, List, Optional

import pytest

from tests._sqlite_session import org_tables, sqlite_session
from tests.test_a_dead_key_neither_refreshes_nor_calls_back import (  # noqa: F401 - fixture
    BASE,
    metadata,
    orgs,
    outbound,
    refresh,
    use,
)


@pytest.mark.parametrize("capabilities, named", [
    (["inventory.read", "tenant.provision"], "org.sync"),
    (["org.sync"], "inventory.read"),
    ([], "inventory.read and org.sync"),
], ids=["without-org-sync", "without-inventory-read", "without-either"])
async def test_a_key_that_cannot_sync_is_not_refreshed(
    monkeypatch: pytest.MonkeyPatch, outbound: List[str], capabilities: List[str], named: str  # noqa: F811
) -> None:
    vault = use(monkeypatch, metadata(capabilities=capabilities))
    db = sqlite_session(*org_tables())
    response = await refresh(db)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "key_cannot_sync" and f"does not hold {named}," in detail["message"]
    assert outbound == [], "a key that cannot sync must not have its callback called"
    assert vault.reads.count(f"{BASE}/secret") <= 1
    assert await orgs(db) == []


async def test_an_add_on_credential_is_not_refreshed(monkeypatch: pytest.MonkeyPatch, outbound: List[str]) -> None:  # noqa: F811
    vault = use(monkeypatch, metadata(extension_install_id=7, app_type="automation"))
    db = sqlite_session(*org_tables())
    response = await refresh(db)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "key_is_add_on"
    assert outbound == [] and vault.reads.count(f"{BASE}/secret") <= 1
    assert await orgs(db) == []


@pytest.mark.parametrize("capabilities", [None, ["inventory.read", "org.sync"]], ids=["legacy", "both"])
async def test_a_key_that_may_sync_still_refreshes(
    monkeypatch: pytest.MonkeyPatch, outbound: List[str], capabilities: Optional[List[str]]  # noqa: F811
) -> None:
    meta: Dict[str, Any] = metadata(capabilities=capabilities)
    if capabilities is None:
        meta.pop("capabilities")
    use(monkeypatch, meta)
    db = sqlite_session(*org_tables())
    response = await refresh(db)
    assert response.status_code == 200, response.text
    assert response.json()["organizations_synced"] == 1 and len(outbound) == 1


async def test_a_dead_key_is_still_answered_by_its_state_first(
    monkeypatch: pytest.MonkeyPatch, outbound: List[str]  # noqa: F811
) -> None:
    use(monkeypatch, metadata(is_revoked=True, capabilities=[]))
    response = await refresh(sqlite_session(*org_tables()))
    assert response.status_code == 409
    assert response.json()["detail"] == "This API key is revoked, inactive or expired, so it cannot be refreshed"
