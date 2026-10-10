"""A credential no installation claims is revoked only once it is older than ORPHAN_MIN_AGE.

An install or a rotation writes its new key to Vault before its row commits. The orphan pass read the
rows, then listed Vault, so a key minted in between was missing from the rows and revoked as an orphan,
breaking an installation that had just been made or rotated. The cutoff is taken before the rows are
read, so a key old enough to be revoked had its row visible to that read. A real orphan is refused at
authentication meanwhile, so waiting a tick or two costs nothing.
"""

import inspect
from datetime import timedelta
from typing import Any, Dict, List

import pytest

from app.core.api_key_expiry import now_utc, utc_isoformat
from app.services import extension_reconcile as sweep
from tests.test_the_reconcile_sweep_narrows_and_reclaims import FakeInstall, _run_orphan_pass


def minted(minutes_ago: float) -> Dict[str, Any]:
    return {"created_at": utc_isoformat(now_utc() - timedelta(minutes=minutes_ago))}


# note: ages, not timestamps: a time computed when the cases are collected is minutes older by the
# time a long run reaches them
@pytest.mark.parametrize("age, revoked", [
    (1, []), (9, []), (11, ["orphan"]), ("not-a-time", ["orphan"]), (None, ["orphan"]),
], ids=["a-minute-old", "nine-minutes-old", "eleven-minutes-old", "unreadable-time", "no-time"])
def test_an_orphan_is_revoked_only_once_it_is_old_enough(
    monkeypatch: pytest.MonkeyPatch, age: Any, revoked: List[str]
) -> None:
    extra: Dict[str, Any] = minted(age) if isinstance(age, int) else ({"created_at": age} if age else {})
    count, vault = _run_orphan_pass(
        installs=[FakeInstall(id=1, organization_id=3, api_key_id="live", granted_scope=[])],
        vault_keys=[{"id": "live", "extension_install_id": 1, **minted(60)},
                    {"id": "orphan", "extension_install_id": 99, **extra}],
        monkeypatch=monkeypatch,
    )
    assert vault.revoked == revoked and count == len(revoked)


def test_a_claimed_key_is_never_revoked_however_old(monkeypatch: pytest.MonkeyPatch) -> None:
    count, vault = _run_orphan_pass(
        installs=[FakeInstall(id=1, organization_id=3, api_key_id="live", granted_scope=[],
                              previous_api_key_id="previous")],
        vault_keys=[{"id": "live", "extension_install_id": 1, **minted(600)},
                    {"id": "previous", "extension_install_id": 1, **minted(600)}],
        monkeypatch=monkeypatch,
    )
    assert vault.revoked == [] and count == 0


def test_the_cutoff_is_taken_before_the_rows_are_read() -> None:
    source = inspect.getsource(sweep._revoke_orphaned_credentials)
    assert source.index("cutoff = now_utc() - ORPHAN_MIN_AGE") < source.index("await db.execute(")
