"""When a stored expiry means "not a credential any more", whatever shape it arrives in.

A stored `expires_at` may be naive UTC, carry an offset, or be unreadable. Comparing aware to
naive raises TypeError and a bad parse raises ValueError, and on the authentication path either
is a 500 rather than a 401; an empty string must not read as "never expires" either. So every
notation parses to an aware UTC instant, a naive value reads as UTC, and anything unreadable
refuses the key.
"""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.api_key_expiry import (
    MalformedExpiry,
    classify_key,
    days_remaining,
    expiry_state,
    is_expired,
    now_utc,
    parse_expiry,
    tally_keys,
    utc_isoformat,
)

BACKEND = Path(__file__).resolve().parents[1]

PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)
FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)


# ── one instant, many notations ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stored",
    [
        "2020-01-01T00:00:00",
        "2020-01-01T00:00:00Z",
        "2020-01-01T00:00:00+00:00",
        "2020-01-01T07:00:00+07:00",  # the same instant, written at UTC+7
        "2020-01-01T00:00:00.123456",
        "2020-01-01T00:00:00.123456Z",
    ],
)
def test_a_past_expiry_refuses_the_key_in_every_notation(stored: str) -> None:
    assert is_expired(stored) is True


@pytest.mark.parametrize(
    "stored",
    [
        "2099-01-01T00:00:00",
        "2099-01-01T00:00:00Z",
        "2099-01-01T00:00:00+00:00",
        "2099-01-01T07:00:00+07:00",
    ],
)
def test_a_future_expiry_accepts_the_key_in_every_notation(stored: str) -> None:
    """An offset on a key years from expiring must not turn into a 500."""
    assert is_expired(stored) is False


def test_the_same_instant_written_two_ways_decides_the_same() -> None:
    """Dropping the offset would move the instant."""
    assert parse_expiry("2020-01-01T07:00:00+07:00") == parse_expiry("2020-01-01T00:00:00Z")


def test_a_naive_value_is_read_as_utc_wherever_the_host_lives() -> None:
    """A naive stored value was written as UTC, so it is read as UTC.

    important: the process timezone is forced here. On a UTC host `astimezone` and
    `replace(tzinfo=utc)` give the same answer, so swapping them would pass unnoticed. Under
    Asia/Jakarta the difference is seven hours in the direction that keeps an expired key working.
    """
    import os
    import time as _time

    original = os.environ.get("TZ")
    try:
        for zone in ("UTC", "Asia/Jakarta", "America/Los_Angeles"):
            os.environ["TZ"] = zone
            _time.tzset()
            assert parse_expiry("2020-01-01T00:00:00") == PAST, f"naive read wrongly under {zone}"
            assert is_expired("2099-01-01T00:00:00") is False, f"future misread under {zone}"
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        _time.tzset()


# ── unreadable values ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stored",
    [
        "", "   ", "not-a-date", "2020-13-45", 12345, [], {}, True,
        "+00:00", "2020-01-01T00:00:00+25:00", "99999-01-01T00:00:00",
        # important: these raise OverflowError from `astimezone`, not ValueError, so an
        # `except ValueError` would not catch them: uncaught on the authentication path, and
        # through the key list one poisoned record would 500 the whole listing for an org.
        "9999-12-31T23:59:59-23:00",
        "0001-01-01T00:00:00+23:00",
    ],
)
def test_an_unreadable_expiry_refuses_the_key(stored) -> None:
    """If the server cannot tell whether a credential is still valid, it is not.

    The empty string matters most: an `if expires_at:` check would skip it and accept a key
    storing "" forever.
    """
    assert is_expired(stored) is True
    with pytest.raises(MalformedExpiry):
        parse_expiry(stored)


def test_nothing_escapes_the_parse_as_a_bare_exception() -> None:
    """Anything unreadable is refused; no exception escapes the parse.

    An escaping exception on this path is a 500 rather than a 401, so the guard has to name every
    exception the conversion can produce.
    """
    hostile = [
        "9999-12-31T23:59:59-23:00", "0001-01-01T00:00:00+23:00", "99999-01-01T00:00:00",
        "2020-01-01T00:00:00+25:00", "+00:00", "", "\x00", "T" * 5000, object(),
    ]
    for value in hostile:
        try:
            assert is_expired(value) is True, f"{value!r} was accepted"
        except MalformedExpiry:
            pytest.fail(f"is_expired let MalformedExpiry escape for {value!r}")
        except Exception as error:  # noqa: BLE001 - the point is that nothing else escapes
            pytest.fail(f"{type(error).__name__} escaped is_expired for {value!r}: {error}")


def test_no_expiry_is_not_an_expired_key() -> None:
    assert is_expired(None) is False
    assert parse_expiry(None) is None
    assert days_remaining(None) is None
    assert expiry_state(None) == "never"


# ── the boundary ───────────────────────────────────────────────────────────────────────────


def test_the_moment_of_expiry_belongs_to_the_expired_side() -> None:
    """A key whose expiry is exactly now has run out. Anything else leaves a window."""
    moment = now_utc()
    assert is_expired(moment.isoformat(), now=moment) is True
    assert is_expired((moment + timedelta(seconds=1)).isoformat(), now=moment) is False


def test_days_remaining_rounds_towards_the_operator() -> None:
    """23 hours left is 0 days, not 1: rounding up would promise a day that is not there."""
    now = now_utc()
    assert days_remaining((now + timedelta(hours=23)).isoformat(), now=now) == 0
    assert days_remaining((now + timedelta(days=1, hours=1)).isoformat(), now=now) == 1
    assert days_remaining((now - timedelta(days=3)).isoformat(), now=now) < 0


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(days=-1), "expired"),
        (timedelta(seconds=-1), "expired"),
        (timedelta(days=1), "expiring"),
        (timedelta(days=14), "expiring"),
        (timedelta(days=40), "active"),
    ],
)
def test_the_state_an_operator_reads(delta, expected) -> None:
    now = now_utc()
    assert expiry_state((now + delta).isoformat(), now=now) == expected


def test_a_new_timestamp_is_written_with_its_offset() -> None:
    """The stored form carries its offset rather than meaning UTC by convention."""
    written = utc_isoformat(FUTURE)
    assert written.endswith("+00:00")
    assert parse_expiry(written) == FUTURE


# ── the ratchet ────────────────────────────────────────────────────────────────────────────


# The functions that decide whether a presented credential still works, or that report that
# decision to an operator. Not the whole file: `last_used_at`, `revoked_at` and the certificate
# validity window are also naive, but they are display or issuance timestamps rather than
# decisions. Covering every `utcnow()` is the job of ruff's `DTZ` rules, which are not enabled.
CREDENTIAL_DECIDERS = (
    ("integrations.py", "validate_api_key"),
    ("integrations.py", "validate_api_key_endpoint"),
    ("integrations.py", "list_api_keys"),
    ("integrations.py", "create_api_key"),
    ("my_keys.py", "list_keys"),
)


def _function(filename: str, name: str):
    tree = ast.parse((BACKEND / "app" / "routers" / filename).read_text())
    return next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        ),
        None,
    )


def test_no_credential_decision_reads_a_naive_clock() -> None:
    """`datetime.utcnow()` cannot be compared with an offset-bearing stored value.

    Python raises rather than guessing, and on the authentication path that would be a 500.
    """
    offenders = []
    for filename, name in CREDENTIAL_DECIDERS:
        function = _function(filename, name)
        assert function is not None, f"{filename}:{name} no longer exists"
        offenders += [
            f"{filename}:{node.lineno} in {name}"
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "utcnow"
        ]
    assert not offenders, (
        "these read a naive UTC clock while deciding whether a credential is still valid, so an "
        "offset-bearing stored value raises TypeError instead of being compared:\n  "
        + "\n  ".join(offenders)
    )


def test_the_expiry_decision_is_made_in_one_place() -> None:
    """One helper decides expiry, so the call sites cannot drift into different failure modes."""
    # Scoped to the API-key deciders. `integrations.py` also parses an expiry for tenant policy
    # assignments, a different subsystem that this does not cover.
    for filename, name in CREDENTIAL_DECIDERS:
        function = _function(filename, name)
        hand_rolled = [
            node.lineno
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "fromisoformat"
        ]
        assert not hand_rolled, (
            f"{filename}:{name} parses an expiry inline at {hand_rolled}; the point of one "
            "helper is that both call sites cannot drift into different failure modes, which is "
            "how one ended up inside a broad except and the other outside any try"
        )

    for name in ("validate_api_key", "validate_api_key_endpoint"):
        function = _function("integrations.py", name)
        calls = {
            getattr(node.func, "id", None)
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
        }
        assert "is_expired" in calls, f"{name} no longer asks whether the key has expired"


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({"is_active": True, "expires_at": None}, "active"),
        ({"is_active": True, "expires_at": "2099-01-01T00:00:00Z"}, "active"),
        # The usual lapsed shape: past its date and still flagged active.
        ({"is_active": True, "expires_at": "2020-01-01T00:00:00"}, "expired"),
        ({"is_active": True, "expires_at": "2020-01-01T00:00:00Z"}, "expired"),
        ({"is_active": True, "expires_at": "not-a-date"}, "expired"),
        ({"is_active": False, "expires_at": None}, "inactive"),
        # Precedence: a revocation is a decision someone made and outranks a date.
        ({"is_revoked": True, "is_active": True, "expires_at": "2099-01-01T00:00:00"}, "revoked"),
        ({"is_revoked": True, "expires_at": "2020-01-01T00:00:00"}, "revoked"),
        # Expiry outranks the flag, because nothing ever writes the flag when a key lapses.
        ({"is_active": False, "expires_at": "2020-01-01T00:00:00"}, "expired"),
        (None, "expired"),
    ],
)
def test_what_a_stored_key_counts_as(metadata, expected) -> None:
    """The tally and the badge read the same function, so they cannot disagree about a key."""
    assert classify_key(metadata) == expected


def test_the_tally_counts_each_state_once() -> None:
    """Each key lands in exactly one state, and a lapsed key is not counted as active.

    Behavioural because a structural check can pass against a broken tally (ex; asserting that
    `expired_count` is incremented stays true if the branch bodies are swapped).
    """
    records = [
        {"is_active": True, "expires_at": None},
        {"is_active": True, "expires_at": "2099-01-01T00:00:00Z"},
        {"is_active": True, "expires_at": "2020-01-01T00:00:00"},
        {"is_active": True, "expires_at": "2020-01-01T00:00:00Z"},
        {"is_active": True, "expires_at": "not-a-date"},
        {"is_revoked": True, "expires_at": "2020-01-01T00:00:00"},
        {"is_active": False, "expires_at": None},
    ]
    assert tally_keys(records) == {"active": 2, "expired": 3, "revoked": 1, "inactive": 1}
    assert sum(tally_keys(records).values()) == len(records), "a key must be counted exactly once"
    assert tally_keys([]) == {"active": 0, "expired": 0, "revoked": 0, "inactive": 0}


def test_the_list_handler_uses_that_tally() -> None:
    """The helper being right is not the same as the handler asking it."""
    tree = ast.parse((BACKEND / "app" / "routers" / "integrations.py").read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "list_api_keys"
    )
    assert any(
        isinstance(node, ast.Call) and getattr(node.func, "id", None) == "tally_keys"
        for node in ast.walk(handler)
    ), "the handler counts inline again, where no fixture can reach it"

    response = next(
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "APIKeyListResponse"
    )
    assert "expired" in {kw.arg for kw in response.keywords}, (
        "the count is computed and then not returned"
    )


def test_a_revoked_and_lapsed_key_still_reports_that_it_lapsed() -> None:
    """`is_expired` is documented as "past its expiration" and an integration branches on it.

    Deriving it from `classify_key` would return False for a revoked key that is also past its
    date, because revocation outranks a date in the badge, and the two fields would then
    contradict each other on the same object.
    """
    from app.routers.integrations import _key_info

    info = _key_info({
        "id": "oak_x", "name": "n", "app_name": "a", "created_at": "2020-01-01T00:00:00",
        "expires_at": "2020-06-01T00:00:00", "is_revoked": True, "is_active": True,
    })
    assert info.is_expired is True, "the field contradicts its own documentation"
    assert info.expiry_state == "expired"
    assert classify_key({"is_revoked": True, "expires_at": "2020-06-01T00:00:00"}) == "revoked", (
        "the badge should still say Revoked, which is the decision someone made"
    )


@pytest.mark.parametrize(
    "builder", ["create", "get", "permissions", "capabilities", "revoke", "list"]
)
def test_every_handler_builds_the_key_the_same_way(builder: str) -> None:
    """Every handler builds APIKeyInfo through `_key_info`, so none can omit the fields derived
    from `expires_at` (ex; answering `expiry_state: "never"` beside a past date)."""
    tree = ast.parse((BACKEND / "app" / "routers" / "integrations.py").read_text())
    direct = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "APIKeyInfo"
    ]
    assert len(direct) <= 1, (
        f"APIKeyInfo is constructed directly at {direct}; use _key_info so a new handler cannot "
        "forget the fields derived from expires_at"
    )


# ── what the create request accepts ────────────────────────────────────────────────────────


@pytest.mark.parametrize("days", [1, 30, 90, 365, 3650])
def test_a_usable_lifetime_is_accepted(days: int) -> None:
    from app.routers.integrations import APIKeyCreateRequest

    request = APIKeyCreateRequest(
        name="k", app_name="a", app_type="mcp", expire_days=days,
        cluster_permissions=[{"cluster_id": "nuc2"}],
    )
    assert request.expire_days == days


@pytest.mark.parametrize("days", [0, -1, -365, 3651, 100000])
def test_a_lifetime_that_means_nothing_is_refused_at_the_boundary(days: int) -> None:
    """Zero is the interesting one: it would mean "expires the instant it is issued".

    A truthy check on `expire_days` would send a zero into the never-expires branch, the
    opposite of what it says.
    """
    import pydantic

    from app.routers.integrations import APIKeyCreateRequest

    with pytest.raises(pydantic.ValidationError):
        APIKeyCreateRequest(
            name="k", app_name="a", app_type="mcp", expire_days=days,
            cluster_permissions=[{"cluster_id": "nuc2"}],
        )


def test_never_expires_is_a_null_not_an_omission() -> None:
    """The dialog sends an explicit null, and the stored value has to be null rather than absent.

    An absent field is how a legacy key is recognised elsewhere in this metadata blob, so the two
    must not blur.
    """
    from app.routers.integrations import APIKeyCreateRequest

    explicit = APIKeyCreateRequest(
        name="k", app_name="a", app_type="mcp", expire_days=None,
        cluster_permissions=[{"cluster_id": "nuc2"}],
    )
    omitted = APIKeyCreateRequest(
        name="k", app_name="a", app_type="mcp",
        cluster_permissions=[{"cluster_id": "nuc2"}],
    )
    assert explicit.expire_days is None and omitted.expire_days is None


def test_the_handler_does_not_test_the_lifetime_for_truthiness() -> None:
    """`if request.expire_days:` and `is not None` agree only while `ge=1` holds.

    They are one constraint change apart from disagreeing about whether a key ever expires, and
    the truthy spelling is the one that silently means "never".
    """
    handler = _function("integrations.py", "create_api_key")
    for node in ast.walk(handler):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "expire_days" in test:
            assert "is not None" in test, (
                f"create_api_key branches on the truthiness of the lifetime: `{test}`"
            )
