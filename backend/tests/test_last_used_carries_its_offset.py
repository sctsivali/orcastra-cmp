"""A stored timestamp has to say which clock it came from.

`last_used_at` was written with a naive `datetime.utcnow().isoformat()`. Nothing failed: the value
was stored, returned, and typed as a plain string all the way to the browser. But a date-time
string with no offset is parsed as local time by JavaScript, so the add-on health panel rendered a
credential used at noon in Jakarta as having been used at five in the morning.

`credential_expires_at` sits directly beside it in the same panel and was always written through
`utc_isoformat`, so the two fields disagreed about what a timestamp means while looking identical.

The assertion here is on the offset rather than on the exact instant, because the instant is right
either way. It was only ever the missing offset that made a reader wrong.
"""

from datetime import datetime

from app.routers.integrations import update_api_key_last_used


class FakeVault:
    """Answers both reads `get_api_key_from_vault` makes and records what is written back."""

    def __init__(self) -> None:
        self.written: dict = {}

    def vault_read(self, path: str) -> dict:
        if path.endswith("/secret"):
            return {"hash": "irrelevant"}
        return {"id": "key-1", "is_active": True, "last_used_at": None}

    def vault_kv_put(self, path: str, data: dict) -> None:
        self.written[path] = data


def _stored_last_used() -> str:
    vault = FakeVault()
    update_api_key_last_used(vault, "key-1")
    assert vault.written, "nothing was written, so the bookkeeping never happened at all"
    stored = next(iter(vault.written.values()))["last_used_at"]
    assert stored is not None, "the field was written as null"
    return stored


def test_the_stored_timestamp_carries_a_utc_offset() -> None:
    stored = _stored_last_used()

    # The positive twin: it must still be a real timestamp, or "it has an offset" is satisfied by
    # any string ending in +00:00.
    parsed = datetime.fromisoformat(stored)
    assert parsed.tzinfo is not None, (
        f"{stored!r} has no offset, so a browser reads it as local time and the health panel "
        "reports the wrong hour"
    )
    assert parsed.utcoffset().total_seconds() == 0, "written in something other than UTC"


def test_it_agrees_with_how_expiry_is_written() -> None:
    """The defect was two adjacent fields disagreeing, so the fix is pinned as agreement."""
    from app.core.api_key_expiry import utc_isoformat

    stored = _stored_last_used()
    reference = utc_isoformat()

    assert stored.endswith("+00:00"), f"{stored!r} is not in the shape utc_isoformat produces"
    assert reference.endswith("+00:00")
