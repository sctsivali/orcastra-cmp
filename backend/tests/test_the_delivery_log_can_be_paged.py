"""The delivery log was a cap, not a page.

`list_deliveries` took `limit` (50, max 200) and no `offset`, so the fifty-first delivery was
unreachable and nothing in the response said the list had been cut. Delivery is at-least-once and
every retry writes a row, so the log outgrows any cap; filtering by state then narrowed a set that
had already been truncated, which answers a different question while looking like the right one.

The same truncation reached the health panel, which reduced over the rows in hand and reported the
result as how many deliveries had given up altogether.

note: this suite is hermetic, so there is no database here. The fake below does not wave the query
through: it reads the offset, the limit and the state predicate off the statement the handler
actually built and applies them itself. A fake that ignored them would pass with the offset deleted,
which is the failure mode this file exists to prevent, so each test was ablated against exactly that.
"""

from typing import Any, Dict, List, Optional

import pytest

from app.routers import extension_webhooks


class _Rows:
    def __init__(self, rows: List[Any]) -> None:
        self._rows = rows

    def all(self) -> List[Any]:
        return self._rows

    def scalars(self) -> "_Rows":
        return self

    def scalar_one(self) -> Any:
        return self._rows[0]


class Delivery:
    """Only the fields the handler copies out."""

    def __init__(self, ident: int, state: str) -> None:
        self.id = ident
        self.subscription_id = 1
        self.event_id = f"evt-{ident}"
        self.event_type = "orcastra.instance.created.v1"
        self.state = state
        self.attempt = 1
        self.response_status = 200 if state == "delivered" else None
        self.response_ms = 12
        self.response_excerpt = None
        self.last_error = None if state == "delivered" else "connection refused"
        self.next_attempt_at = None
        self.created_at = None
        self.payload = {"id": ident}


class FakeDB:
    """Answers the four statements the handler issues, honouring what each one asks for.

    Dispatch is on the compiled SQL rather than on call order, so reordering the handler does not
    silently change which canned answer a query receives.
    """

    def __init__(self, rows: List[Delivery], subscription_ids: List[int]) -> None:
        self.rows = rows
        self.subscription_ids = subscription_ids
        self.counts_statement_filtered_by_state: Optional[bool] = None

    async def execute(self, statement: Any) -> _Rows:
        compiled = statement.compile()
        sql = str(compiled).replace("\n", " ")
        params: Dict[str, Any] = dict(compiled.params)
        state = next(
            (v for k, v in params.items() if k.startswith("state_") and isinstance(v, str)),
            None,
        )

        if "webhook_subscriptions.id" in sql and "webhook_deliveries" not in sql:
            return _Rows([(sid,) for sid in self.subscription_ids])

        if "GROUP BY" in sql:
            # Recorded rather than asserted here so the assertion lives in the test that cares.
            self.counts_statement_filtered_by_state = state is not None
            tally: Dict[str, int] = {}
            for row in self.rows:
                tally[row.state] = tally.get(row.state, 0) + 1
            return _Rows(list(tally.items()))

        matching = [r for r in self.rows if state is None or r.state == state]

        if "count(" in sql.lower():
            return _Rows([len(matching)])

        offset = statement._offset or 0
        limit = statement._limit
        window = matching[offset : offset + limit] if limit is not None else matching[offset:]
        return _Rows(window)


@pytest.fixture(autouse=True)
def _skip_the_authorisation_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scope check is covered by its own test; this file is about paging."""

    async def _allowed(_db: Any, _install_id: int, _user: Any) -> object:
        return object()

    monkeypatch.setattr(extension_webhooks, "_install_for", _allowed)


def _log(delivered: int = 0, failed: int = 0, dead: int = 0) -> List[Delivery]:
    rows: List[Delivery] = []
    ident = 1000
    for state, count in (("delivered", delivered), ("failed", failed), ("dead", dead)):
        for _ in range(count):
            rows.append(Delivery(ident, state))
            ident -= 1
    return rows


async def _page(db: FakeDB, **kwargs: Any) -> Any:
    return await extension_webhooks.list_deliveries(
        install_id=1, user=object(), db=db, **{"state": None, "limit": 50, "offset": 0, **kwargs}
    )


@pytest.mark.asyncio
async def test_a_second_page_is_reachable_and_is_not_the_first_one_again() -> None:
    db = FakeDB(_log(delivered=120), subscription_ids=[1])

    first = await _page(db, limit=50, offset=0)
    second = await _page(db, limit=50, offset=50)

    assert len(first.deliveries) == 50
    assert len(second.deliveries) == 50
    first_ids = [d["id"] for d in first.deliveries]
    second_ids = [d["id"] for d in second.deliveries]
    assert not set(first_ids) & set(second_ids), (
        "page two repeats page one, so the offset never reached the query and every delivery past "
        "the first window is still unreachable"
    )
    assert first.has_more is True
    assert first.total == 120


@pytest.mark.asyncio
async def test_the_last_page_reports_no_more_and_an_offset_past_the_end_is_empty() -> None:
    db = FakeDB(_log(delivered=60), subscription_ids=[1])

    last = await _page(db, limit=50, offset=50)
    assert len(last.deliveries) == 10
    assert last.has_more is False, "the final page still invites the reader to a page that is not there"

    beyond = await _page(db, limit=50, offset=500)
    assert beyond.deliveries == []
    assert beyond.has_more is False
    assert beyond.total == 60, "the total collapsed with the page, so the pager loses its divisor"


@pytest.mark.asyncio
async def test_total_follows_the_state_filter_and_counts_deliberately_does_not() -> None:
    """The two numbers answer different questions, which is why both are returned.

    `total` divides the pager, so it has to match what the filter selected. `counts` is what the
    health panel reads to say how much has failed on this installation, so a filter the operator
    happens to have applied must not change it.
    """
    db = FakeDB(_log(delivered=30, failed=8, dead=2), subscription_ids=[1])

    unfiltered = await _page(db)
    assert unfiltered.total == 40
    assert unfiltered.counts == {"delivered": 30, "failed": 8, "dead": 2}

    filtered = await _page(db, state="dead")
    assert filtered.total == 2, "the pager would divide by the wrong number for a filtered log"
    assert filtered.counts == {"delivered": 30, "failed": 8, "dead": 2}, (
        "counts moved with the filter, so the health panel would report two dead deliveries as the "
        "whole story whenever an operator was looking at the dead ones"
    )
    assert db.counts_statement_filtered_by_state is False, (
        "the counts query carried the state predicate, so its answer is scoped to one state"
    )


@pytest.mark.asyncio
async def test_an_installation_with_no_subscription_still_answers_the_envelope() -> None:
    """It used to return a bare list, so the caller's total and counts would be undefined."""
    db = FakeDB([], subscription_ids=[])

    page = await _page(db)

    assert page.deliveries == []
    assert page.total == 0
    assert page.counts == {}
    assert page.has_more is False


def _bound(parameter: Any, kind: str) -> Optional[int]:
    """Read a numeric bound off a FastAPI Query, whichever shape this version uses.

    important: the bounds live on the object itself in some releases and in annotated-types entries
    under `.metadata` in others, and this repo runs a different FastAPI in the venv than in the
    image. Reading only one shape would pass here and assert nothing against the deployed one.
    Returning None when neither carries it makes that an outright failure rather than a silent pass.
    """
    direct = getattr(parameter, kind, None)
    if direct is not None:
        return direct
    for entry in getattr(parameter, "metadata", ()) or ():
        value = getattr(entry, kind, None)
        if value is not None:
            return value
    return None


def test_the_limit_is_bounded_at_both_ends() -> None:
    """`ge=1` was missing, so limit=0 was accepted and paged through nothing without erroring."""
    import inspect

    signature = inspect.signature(extension_webhooks.list_deliveries)
    limit = signature.parameters["limit"].default
    offset = signature.parameters["offset"].default

    assert _bound(limit, "ge") == 1, "limit accepts zero, which returns an empty page forever"
    assert _bound(limit, "le") == 200, "the cap that keeps one request from reading the whole log"
    assert _bound(offset, "ge") == 0, "a negative offset would reach the database"
