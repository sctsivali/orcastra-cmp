"""Every feature whose safety depends on a daemon fix reads its minimum build from one table.

Each Incus line has its own floor: the LTS builds 6.0.7 and 7.0.2 carry the 7.3 symlink fixes as
backports, so a single feature-track floor would refuse them.

Evidence for the Incus rows, from github.com/lxc/incus:
- image symlinks (Incus 7.3.0): on stable-7.0 as 136444d9b and 001bd0657, first tagged in v7.0.2
  (v7.0.1 predates them); on stable-6.0 as 62d944bf9, first tagged in v6.0.7.
- security baseline (the advisory batch with ranges "< v7.6.0"): backported to both LTS branches
  after their newest tags, so the first patched LTS builds are 6.0.8 and 7.0.3.
"""

import pytest

from app.core.engine_baseline import floor_verdict, parse_version


@pytest.mark.parametrize(
    "version,allowed",
    [
        ("7.3.0", True),
        ("7.6", True),
        ("7.2.0", False),
        ("7.0.2", True),
        ("7.0.1", False),
        ("6.0.7", True),
        ("6.0.6", False),
        ("6.23.0", False),
    ],
)
def test_incus_files_follow_the_symlink_fixes_on_every_line(version, allowed):
    assert floor_verdict("instance_files", "incus", version).allowed is allowed


@pytest.mark.parametrize(
    "version,allowed,minimum",
    [
        ("7.6", True, "7.6.0"),
        ("7.6.0", True, "7.6.0"),
        ("7.5.1", False, "7.6.0"),
        ("7.0.2", False, "7.0.3"),
        ("7.0.3", True, "7.0.3"),
        ("6.0.7", False, "6.0.8"),
        ("6.0.8", True, "6.0.8"),
        ("6.1.0", False, "7.6.0"),
    ],
)
def test_the_incus_security_baseline_names_the_build_that_fixes_each_line(version, allowed, minimum):
    verdict = floor_verdict("security_baseline", "incus", version)
    assert verdict.allowed is allowed
    assert verdict.minimum == minimum


def test_the_oldest_member_decides_a_mixed_cluster():
    verdict = floor_verdict("security_baseline", "incus", "7.6", member_versions=["7.6", "7.5.1", "7.6"])
    assert verdict.allowed is False
    assert verdict.seen == "7.5.1"


def test_an_unreadable_version_is_refused_not_guessed():
    verdict = floor_verdict("security_baseline", "incus", "not-a-version")
    assert verdict.allowed is False
    assert verdict.code == "unknown_version"


def test_a_feature_without_a_floor_for_the_engine_says_so():
    """LXD has no client-key floor wired; the caller must not read that as a refusal."""
    verdict = floor_verdict("client_keys", "lxd", "5.21.8")
    assert verdict.allowed is True
    assert verdict.code == "no_floor"


def test_an_unknown_feature_is_a_programming_error():
    with pytest.raises(KeyError):
        floor_verdict("no_such_feature", "incus", "7.6")


@pytest.mark.parametrize("raw,parsed", [("7.6", (7, 6, 0)), ("5.21.8", (5, 21, 8)), ("6.0.7-1", (6, 0, 7)), ("x", None)])
def test_versions_parse_like_the_daemons_print_them(raw, parsed):
    assert parse_version(raw) == parsed


def test_the_reason_names_the_engine_and_the_minimum():
    verdict = floor_verdict("security_baseline", "incus", "7.0.2")
    assert "Incus 7.0.2" in verdict.reason
    assert "7.0.3" in verdict.reason
