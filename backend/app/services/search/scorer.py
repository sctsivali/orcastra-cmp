"""Relevance scoring for global search (pure, dependency-free, unit-tested).

Substring matching with a relevance tier, the infra-console norm: operators
search near-exact resource names, so deterministic ranking beats fuzzy guessing
(a wrong match in a console that can stop/delete instances is dangerous).

Tiers (before field weighting):
    exact            100
    prefix            80
    word-boundary     65   (query starts a word, after a non-alphanumeric break)
    substring         50
    no match           0
"""

from typing import Iterable, Optional, Tuple

EXACT = 100
PREFIX = 80
WORD_BOUNDARY = 65
SUBSTRING = 50

# Field weights - which field a match landed in matters as much as how it matched.
FieldSpec = Tuple[Optional[str], float, str]  # (value, weight, field_name)


def _word_boundary_match(value: str, query: str) -> bool:
    """True if ``query`` begins a word inside ``value`` (start, or after a break)."""
    idx = value.find(query)
    while idx != -1:
        if idx == 0 or not value[idx - 1].isalnum():
            return True
        idx = value.find(query, idx + 1)
    return False


def score_field(query: str, value: Optional[str], weight: float = 1.0) -> int:
    """Score a single field. Case-insensitive, None/empty-safe. Returns >= 0."""
    if not query or not value:
        return 0
    q = query.strip().lower()
    v = value.strip().lower()
    if not q or not v:
        return 0

    if v == q:
        base = EXACT
    elif v.startswith(q):
        base = PREFIX
    elif _word_boundary_match(v, q):
        base = WORD_BOUNDARY
    elif q in v:
        base = SUBSTRING
    else:
        return 0
    return round(base * weight)


def best_score(query: str, fields: Iterable[FieldSpec]) -> Tuple[int, Optional[str]]:
    """Return the best (score, field_name) across all of a candidate's fields."""
    best = 0
    best_field: Optional[str] = None
    for value, weight, name in fields:
        s = score_field(query, value, weight)
        if s > best:
            best, best_field = s, name
    return best, best_field
