"""An audit trail must not lose its tail to a page size.

GET /console/sessions/audit took `limit` and nothing else. The Sessions page asked for 500
while the table held 660 rows, so the oldest 160 were unreachable from the UI and the pager on
that screen was paging a set that had already been truncated underneath it. Nothing said so:
the response was a bare array, so a short page and a lost page looked identical.

These pin the two halves of the fix. `offset` has to reach rows past `limit`, and `total` has
to be counted over the same filters as the page rather than inferred from the rows returned,
because `len(rows)` is exactly the number that cannot tell those two cases apart.
"""

import asyncio

import pytest

from app.core.dependencies import AuthenticatedUser
from app.routers import console


class _Row:
    """The columns the serializer reads. Anything absent would raise, so this is the contract."""

    _FIELDS = (
        "id session_id event_type instance_name host project protocol user_id user_email "
        "user_role duration_seconds download_bytes upload_bytes target_organization_id "
        "target_org_slug break_glass_grant_id"
    ).split()

    def __init__(self, row_id):
        for field in self._FIELDS:
            setattr(self, field, None)
        self.id = row_id
        self.session_id = f"s-{row_id}"
        self.event_type = "connect"
        self.user_email = "qa@example.com"
        self.created_at = None


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    """Answers the count query with a fixed total and the page query by slicing.

    important: it reads offset and limit off the compiled statement rather than trusting the
    handler to have been called correctly. A fake that returned a fixed page would pass whether
    or not offset was ever applied, which is the failure this file exists to catch.
    """

    def __init__(self, total):
        self.total = total
        self.all_rows = [_Row(i) for i in range(total)]
        self.seen = []

    async def execute(self, statement):
        compiled = str(statement)
        if "count(" in compiled:
            return _Result(scalar=self.total)
        offset = statement._offset or 0
        limit = statement._limit
        self.seen.append((offset, limit))
        return _Result(rows=self.all_rows[offset : offset + limit])


def _call(db, monkeypatch, *, limit, offset, allowed=None):
    class _Maker:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(console, "async_session_maker", _Maker())

    import app.core.org_scope as org_scope

    monkeypatch.setattr(org_scope, "get_current_allowed_clusters", lambda: allowed)

    return asyncio.run(
        console.query_session_audit_logs(
            instance_name=None,
            event_type=None,
            scope="self",
            limit=limit,
            offset=offset,
            _rbac=AuthenticatedUser(user_id="u1", email="qa@example.com"),
        )
    )


def test_a_page_past_the_limit_returns_the_rows_the_old_cap_could_not_reach(monkeypatch):
    db = _FakeDB(total=660)

    page = _call(db, monkeypatch, limit=100, offset=600)

    assert db.seen == [(600, 100)], "offset must reach the query, not just the signature"
    assert [row["id"] for row in page["logs"]] == list(range(600, 660))
    assert page["offset"] == 600


def test_total_is_counted_over_the_filters_rather_than_read_off_the_page(monkeypatch):
    db = _FakeDB(total=660)

    page = _call(db, monkeypatch, limit=100, offset=0)

    assert page["total"] == 660, "the whole trail, not the 100 rows on this page"
    assert len(page["logs"]) == 100


@pytest.mark.parametrize(
    "offset,expected",
    [(0, True), (500, True), (600, False)],
)
def test_has_more_marks_the_last_page_and_only_the_last_page(monkeypatch, offset, expected):
    db = _FakeDB(total=660)

    page = _call(db, monkeypatch, limit=100, offset=offset)

    assert page["has_more"] is expected


def test_the_response_is_a_page_envelope_and_not_a_bare_array(monkeypatch):
    # A revert to the old shape would break every caller silently, because a list is truthy.
    db = _FakeDB(total=3)

    page = _call(db, monkeypatch, limit=100, offset=0)

    assert isinstance(page, dict)
    assert set(page) == {"logs", "total", "limit", "offset", "has_more"}
