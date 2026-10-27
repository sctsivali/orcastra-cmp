"""The release manifest is read from a public repository and unsigned, so it is checked like any
other input before it is believed (services/release_check/manifest.py). One that fails is not used.
"""

import copy

import pytest

from app.services.release_check.manifest import MAX_BYTES, ManifestInvalid, parse
from tests._release_manifest import PUBLISHED, body


def _with(change):
    data = copy.deepcopy(PUBLISHED)
    change(data)
    return body(data)


def test_the_published_manifest_parses() -> None:
    manifest = parse(body())
    assert manifest.latest.version == "1.0.0-RC4-hotfix3"
    assert manifest.find("v1.0.0-RC4-hotfix2-abcdef1").security is True


def test_unknown_fields_are_ignored() -> None:
    manifest = parse(_with(lambda d: d["releases"][0].update(script="<b>", flag=1)))
    assert manifest.latest.version == "1.0.0-RC4-hotfix3"


@pytest.mark.parametrize("change, message", [
    (lambda d: d.update(schema_version=2), "schema_version"),
    (lambda d: d.update(releases=[]), "non-empty"),
    (lambda d: d["releases"].reverse(), "newest first"),
    (lambda d: d["releases"].insert(1, dict(d["releases"][1])), "newest first"),
    (lambda d: d["releases"][1].update(status="latest"), "exactly one"),
    (lambda d: d["releases"][0].update(status="supported"), "exactly one"),
    (lambda d: d["releases"][4].update(status="supported"), "better state"),
    (lambda d: d["releases"][0].update(status="current"), "status must be one of"),
    (lambda d: d["releases"][0].update(version="1.0.0-rc5"), "not an Orcastra version"),
    (lambda d: d["releases"][0].update(released="9 Oct"), "must be a date"),
    (lambda d: d["releases"][0].update(security="yes"), "true or false"),
    (lambda d: d["releases"][0].update(notes_url="https://evil.example/notes"), "documentation"),
    (lambda d: d["releases"][0].update(notes_url="http://docs.orcastra.io/en/latest/"), "documentation"),
    (lambda d: d["releases"][0].update(upgrade_url="https://github.com/someone-else/orcastra-docs"), "orcastra-docs"),
    (lambda d: d["releases"][0].update(upgrade_url="javascript:alert(1)"), "documentation"),
    (lambda d: d["releases"][0].update(notes_url="https://docs.orcastra.io:99999/x"), "documentation"),
    (lambda d: d["releases"][0].update(notes_url="https://docs.orcastra.io:abc/x"), "documentation"),
])
def test_a_manifest_that_breaks_a_rule_is_refused(change, message) -> None:
    with pytest.raises(ManifestInvalid, match=message):
        parse(_with(change))


def test_size_and_shape_limits() -> None:
    with pytest.raises(ManifestInvalid, match="64 KiB"):
        parse(b" " * (MAX_BYTES + 1))
    with pytest.raises(ManifestInvalid, match="not JSON"):
        parse(b"<html>")
    with pytest.raises(ManifestInvalid, match="object"):
        parse(b"[]")
