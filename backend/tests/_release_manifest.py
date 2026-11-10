"""A published release manifest, for the release notice tests."""

import json

DOCS = "https://docs.orcastra.io/en/latest/operations"


def release(version, status, released, *, security=False):
    anchor = version.replace(".", "-").lower()
    return {
        "version": version,
        "status": status,
        "released": released,
        "security": security,
        "notes_url": f"{DOCS}/releases/#{anchor}",
        "upgrade_url": f"{DOCS}/upgrading/",
    }


PUBLISHED = {
    "schema_version": 1,
    "product": "orcastra-cmp",
    "generated_at": "2026-10-27T00:00:00Z",
    "releases": [
        release("1.0.0-RC4-hotfix3", "latest", "2026-10-09"),
        release("1.0.0-RC4-hotfix2", "deprecated", "2026-09-24", security=True),
        release("1.0.0-RC4-hotfix1", "deprecated", "2026-09-17"),
        release("1.0.0-RC4", "deprecated", "2026-08-28"),
        release("1.0.0-RC3", "end_of_life", "2026-08-01"),
        release("1.0.0-RC2", "end_of_life", "2026-07-01"),
        release("1.0.0-RC1", "end_of_life", "2026-06-01"),
    ],
}


def body(data=None) -> bytes:
    return json.dumps(PUBLISHED if data is None else data).encode()
