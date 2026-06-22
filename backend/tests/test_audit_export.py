"""Unit tests for the audit-log export helpers (app.routers.audit_log).

Covers the shared filter builder and the CSV/JSON value serialization used by the
streamed export endpoint.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from app.routers.audit_log import (
    _audit_conditions,
    _csv_value,
    _json_value,
    _EXPORT_COLUMNS,
    EXPORT_MAX_ROWS,
)


def _row():
    return SimpleNamespace(
        id=7,
        occurred_at=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        details={"k": "v"},
        source_ip=None,
        action="audit.export",
    )


def test_no_filters_yields_no_conditions():
    assert _audit_conditions(None, None, None, None, None) == []


def test_all_filters_yield_five_conditions():
    conds = _audit_conditions("a", "b", "success", "2026-01-01", "2026-01-02")
    assert len(conds) == 5


def test_csv_value_serialization():
    row = _row()
    assert _csv_value(row, "occurred_at") == row.occurred_at.isoformat()
    assert _csv_value(row, "details") == '{"k": "v"}'
    assert _csv_value(row, "source_ip") == ""
    assert _csv_value(row, "id") == 7


def test_json_value_keeps_native_types():
    row = _row()
    assert _json_value(row, "details") == {"k": "v"}
    assert isinstance(_json_value(row, "occurred_at"), str)
    assert _json_value(row, "source_ip") is None


def test_columns_and_cap_sane():
    assert "details" in _EXPORT_COLUMNS
    assert "occurred_at" in _EXPORT_COLUMNS
    assert "event_id" in _EXPORT_COLUMNS
    assert EXPORT_MAX_ROWS >= 1000


if __name__ == "__main__":
    test_no_filters_yields_no_conditions()
    test_all_filters_yield_five_conditions()
    test_csv_value_serialization()
    test_json_value_keeps_native_types()
    test_columns_and_cap_sane()
    print("audit_export tests passed")
