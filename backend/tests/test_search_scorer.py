"""Unit tests for the global-search relevance scorer (pure, no DB)."""

from app.services.search.scorer import (
    EXACT,
    PREFIX,
    SUBSTRING,
    WORD_BOUNDARY,
    best_score,
    score_field,
)


def test_tier_ordering():
    assert EXACT > PREFIX > WORD_BOUNDARY > SUBSTRING
    assert score_field("nginx", "nginx") == EXACT
    assert score_field("ngin", "nginx-01") == PREFIX
    assert score_field("01", "nginx-01") == WORD_BOUNDARY  # '01' begins a word after '-'
    assert score_field("in", "nginx") == SUBSTRING  # mid-word substring


def test_case_insensitive():
    assert score_field("NGINX", "nginx") == EXACT
    assert score_field("nginx", "NGINX") == EXACT
    assert score_field("Web", "web-01") == PREFIX


def test_none_and_empty_safe():
    assert score_field("", "nginx") == 0
    assert score_field("nginx", None) == 0
    assert score_field("nginx", "") == 0
    assert score_field("   ", "nginx") == 0


def test_no_match():
    assert score_field("zzz", "nginx") == 0


def test_field_weight_applied():
    assert score_field("web", "web", 0.5) == round(EXACT * 0.5)
    assert score_field("web", "web", 1.0) == EXACT


def test_word_boundary_vs_substring():
    assert score_field("jakarta", "loc_jakarta") == WORD_BOUNDARY
    assert score_field("akart", "loc_jakarta") == SUBSTRING


def test_best_score_picks_highest_weighted_field():
    fields = [("nginx-01", 1.0, "name"), ("web", 0.5, "tag")]
    score, field = best_score("web", fields)
    assert field == "tag" and score == round(EXACT * 0.5)
    score, field = best_score("nginx", fields)
    assert field == "name" and score == PREFIX
    score, field = best_score("zzz", fields)
    assert score == 0 and field is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} passed")
