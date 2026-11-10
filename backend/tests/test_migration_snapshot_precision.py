"""A cross-cluster transfer keeps snapshots at whole-second precision, so the inventory does too.

The source reads ``08:05:55.928374805Z`` and the target ``08:05:55Z`` for the same snapshot, so a
full-precision comparison would stop every cross-cluster move with snapshots in reconciliation.
"""

from __future__ import annotations

from typing import Any

from app.services.migrations import executor


class _Snapshots:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def get(self, **_kwargs: Any) -> Any:
        return type("R", (), {"json": lambda _self: {"metadata": self.rows}})()


def _client(rows: list[dict[str, Any]]) -> Any:
    instance = type("I", (), {"snapshots": _Snapshots(rows)})()
    return type("C", (), {"api": type("A", (), {"instances": {"web": instance}})()})()


def _fingerprint(created: str, expires: str = "0001-01-01T00:00:00Z") -> str:
    rows = [{"name": "snap0", "created_at": created, "expires_at": expires, "stateful": False}]
    return executor.snapshot_inventory_fingerprint(_client(rows), project="p", name="web")


def test_sub_second_and_offset_forms_of_one_moment_match() -> None:
    assert _fingerprint("2026-10-16T08:05:55.928374805Z") == _fingerprint("2026-10-16T08:05:55Z")
    assert _fingerprint("2026-10-16T10:05:55.5+02:00") == _fingerprint("2026-10-16T08:05:55Z")


def test_another_second_or_another_snapshot_still_differs() -> None:
    assert _fingerprint("2026-10-16T08:05:56Z") != _fingerprint("2026-10-16T08:05:55Z")
    assert _fingerprint("2026-10-16T08:05:55Z", "2026-11-01T00:00:00Z") != _fingerprint("2026-10-16T08:05:55Z")
