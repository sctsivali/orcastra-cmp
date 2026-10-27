"""Orcastra versions order by number, not by text (services/release_check/version_order.py)."""

import pytest

from app.services.release_check.version_order import canonical, key

ORDER = [
    "1.0.0-RC3",
    "1.0.0-RC4",
    "1.0.0-RC4-hotfix1",
    "1.0.0-RC4-hotfix2",
    "1.0.0-RC4-hotfix10",
    "1.0.0-RC5",
    "1.0.0-RC10",
    "1.0.0",
    "1.0.0-hotfix1",
    "1.0.1",
    "1.10.0",
]


def test_versions_sort_by_number_and_a_final_release_after_its_rcs() -> None:
    assert sorted(reversed(ORDER), key=key) == ORDER


@pytest.mark.parametrize("raw, expected", [
    ("v1.0.0-RC4-hotfix1", "1.0.0-RC4-hotfix1"),
    ("1.0.0-RC4-hotfix1-a90d8de", "1.0.0-RC4-hotfix1"),
    (" 1.0.0-RC5 ", "1.0.0-RC5"),
])
def test_a_leading_v_and_a_trailing_commit_are_ignored(raw: str, expected: str) -> None:
    assert canonical(raw) == expected
    assert key(raw) == key(expected)


@pytest.mark.parametrize("raw", ["", None, "dev", "1.0", "1.0.0-rc4", "1.0.0-RC4-hotfix", "latest"])
def test_anything_else_is_not_a_version(raw) -> None:
    assert key(raw) is None
    assert canonical(raw) is None
