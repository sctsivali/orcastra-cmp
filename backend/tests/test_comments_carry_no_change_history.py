"""Comments and docstrings explain the code; change history belongs in commits.

Same rules as scripts/checks/check-comment-hygiene.mjs: no decision credits, review references,
calendar dates or release-round labels, and no `note:` prefix. Alembic revision headers keep their
generated `Create Date`.
"""

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
SCANNED = ("app", "tests", "alembic", "scripts")

RULES = {
    "decision credit": re.compile(
        r"\bowner (decision|decided|asked|ruling)\b|\(owner,? 20\d\d|\bdecision D\d+\b", re.I
    ),
    "review reference": re.compile(
        r"\b(security|code|design) review\b[^\n]{0,40}(\b20\d\d\b|\bfinding \d|\bv\d)", re.I
    ),
    "release round": re.compile(
        r"\bRC\d+[ -](acceptance|follow-?ups?|hotfix|outage)\b|\bFU-\d+\b|\bround \d+\b", re.I
    ),
    "note: prefix": re.compile(r"^note:", re.I),
}
DATE = re.compile(r"\b20\d\d-[01]\d-[0-3]\d\b(?!T\d)")
ALEMBIC_HEADER = re.compile(r"^(Create Date|Revision ID|Revises):")


def violations(text: str) -> list[str]:
    found = set()
    for raw in text.splitlines():
        line = re.sub(r"^\s*#+\s?", "", raw).strip()
        if ALEMBIC_HEADER.match(line):
            continue
        found.update(name for name, rule in RULES.items() if rule.search(line))
        for m in DATE.finditer(line):
            before = line[: m.start()]
            if len(re.findall(r"[`'\"]", before)) % 2 == 0:
                found.add("date")
    return sorted(found)


def _comments_and_docstrings(source: str):
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            yield tok.start[0], tok.string
    for node in ast.walk(ast.parse(source)):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(node, field, None)
            for stmt in body if isinstance(body, list) else []:
                if (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    yield stmt.lineno, stmt.value.value


@pytest.mark.parametrize(
    "text, expected",
    [
        ("# Read-only past the term (owner decision 2026-10-27).", ["date", "decision credit"]),
        ("Security review of v1.0.0-RC4, finding 1.", ["review reference"]),
        ("# RC5 acceptance FU-3 moved this.", ["release round"]),
        ("# note: the cache is per worker.", ["note: prefix"]),
        ("# important: the owner key decides who may rotate.", []),
        ("Parses `2026-01-31` style dates.", []),
        ("Timestamps look like 2026-01-31T10:00:00Z.", []),
        ("Create Date: 2026-01-31 10:00:00.000000", []),
    ],
)
def test_the_rules_match_what_they_should(text, expected) -> None:
    assert violations(text) == expected


def test_no_comment_or_docstring_carries_change_history() -> None:
    failures = []
    files = 0
    for top in SCANNED:
        for path in sorted((BACKEND / top).rglob("*.py")):
            files += 1
            for line, text in _comments_and_docstrings(path.read_text(encoding="utf-8")):
                broken = violations(text)
                if broken:
                    first = text.strip().splitlines()[0][:100] if text.strip() else ""
                    failures.append(f"{path.relative_to(BACKEND)}:{line} {', '.join(broken)}: {first}")
    assert files > 100, "the scan walked almost nothing; check SCANNED"
    assert not failures, "history belongs in commits, not comments:\n  " + "\n  ".join(failures)
