"""
A rebuild of this release line installs the exact set it was tested with.

requirements.txt only carries lower bounds, and the base image was a floating tag, so every rebuild
used to resolve whatever the index and the registry published that day. The shipped build then
differed from the tested one in ways no test of ours would see. constraints.txt pins the set the
RC5 image resolved, requirements.txt keeps the readable ranges and their ceilings, and the Dockerfile
pins the base image by digest.
"""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REQUIREMENTS = BACKEND / "requirements.txt"
CONSTRAINTS = BACKEND / "constraints.txt"
DOCKERFILE = BACKEND / "Dockerfile"

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][^\s;]*)$")


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in CONSTRAINTS.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.match(line)
        assert match, f"constraints.txt line is not name==version: {line!r}"
        pins[_normalise(match.group(1))] = match.group(2)
    return pins


def _requirement_names() -> list[str]:
    names: list[str] = []
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        names.append(_normalise(re.split(r"[\[<>=!~ ;]", line, maxsplit=1)[0]))
    return names


def test_requirements_apply_the_constraints() -> None:
    lines = [line.strip() for line in REQUIREMENTS.read_text().splitlines()]
    assert "-c constraints.txt" in lines


def test_every_requirement_has_an_exact_pin() -> None:
    pins = _pins()
    missing = [name for name in _requirement_names() if name not in pins]
    assert not missing, f"requirements without a pin in constraints.txt: {missing}"


def test_the_base_image_is_pinned_by_digest() -> None:
    first_from = next(
        line for line in DOCKERFILE.read_text().splitlines() if line.strip().upper().startswith("FROM ")
    )
    assert "@sha256:" in first_from, first_from


def test_the_constraints_reach_the_install_layer() -> None:
    text = DOCKERFILE.read_text()
    copy_at = next(
        (i for i, line in enumerate(text.splitlines()) if line.startswith("COPY") and "constraints.txt" in line),
        None,
    )
    install_at = next(
        i for i, line in enumerate(text.splitlines()) if "pip install" in line and "requirements.txt" in line
    )
    assert copy_at is not None and copy_at < install_at
