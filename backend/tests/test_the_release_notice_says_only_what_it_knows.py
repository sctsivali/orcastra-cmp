"""What an Administrator is told for each running version, against the manifest as published
(services/release_check/comparator.py). A build this cannot place is told nothing.
"""

import copy

import pytest

from app.services.release_check.comparator import evaluate
from app.services.release_check.manifest import parse
from tests._release_manifest import PUBLISHED, body, release

MANIFEST = parse(body())


@pytest.mark.parametrize("running, level, reasons, security", [
    ("1.0.0-RC4-hotfix3", "none", (), None),
    ("1.0.0-RC4-hotfix2", "warning", ("deprecated", "newer_release"), None),
    ("v1.0.0-RC4-hotfix2-a90d8de", "warning", ("deprecated", "newer_release"), None),
    ("1.0.0-RC4-hotfix1", "critical", ("security_fix", "deprecated", "newer_release"), "1.0.0-RC4-hotfix2"),
    ("1.0.0-RC4", "critical", ("security_fix", "deprecated", "newer_release"), "1.0.0-RC4-hotfix2"),
    ("1.0.0-RC3", "critical", ("end_of_life", "security_fix", "newer_release"), "1.0.0-RC4-hotfix2"),
])
def test_each_published_version_gets_its_notice(running, level, reasons, security) -> None:
    verdict = evaluate(running, MANIFEST)
    assert (verdict.level, verdict.reasons, verdict.security_version) == (level, reasons, security)
    assert verdict.latest_version == "1.0.0-RC4-hotfix3"
    assert verdict.upgrade_url.startswith("https://docs.orcastra.io/")


@pytest.mark.parametrize("running, reason", [
    ("", "unstamped"),
    (None, "unstamped"),
    ("dev", "unstamped"),
    ("1.0.0-RC5", "ahead_of_feed"),
    ("1.0.0-RC4-hotfix9", "ahead_of_feed"),
    ("0.9.0", "not_in_feed"),
])
def test_a_build_this_cannot_place_is_told_nothing(running, reason) -> None:
    verdict = evaluate(running, MANIFEST)
    assert (verdict.level, verdict.reasons) == ("none", (reason,))


def test_a_supported_older_release_is_told_about_the_newer_one() -> None:
    data = copy.deepcopy(PUBLISHED)
    data["releases"] = [release("1.0.0-RC5", "latest", "2026-11-01"), release("1.0.0-RC4-hotfix3", "supported", "2026-10-09")]
    verdict = evaluate("1.0.0-RC4-hotfix3", parse(body(data)))
    assert (verdict.level, verdict.reasons, verdict.running_status) == ("info", ("newer_release",), "supported")
