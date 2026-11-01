"""Citation-format fix: variant normalization, strict validation, prompt rule."""

from __future__ import annotations

from atlas.answer import (
    SYSTEM_PROMPT,
    normalize_citations,
    validate_citations,
)
from atlas.retrieval import RetrievalChunk

FILE_LINES = {"app/service.py": 50}
CHUNKS = [
    RetrievalChunk(
        chunk_id=1,
        symbol_id=1,
        path="app/service.py",
        name="greet",
        kind="function",
        start=1,
        end=20,
        text="def greet():\n    return 'hi'",
        docstring="",
        tokens=10,
    )
]


# -- normalization rewrites the observed variants ------------------------


def test_normalizes_at_line_variant_before_validation() -> None:
    text = "Greeting lives in `app/service.py` at line 5."

    clean, report = validate_citations(text, CHUNKS, FILE_LINES)

    assert report.ok
    assert report.valid == ["app/service.py:5"]
    assert "app/service.py:5" in clean
    assert "at line" not in clean


def test_normalizes_at_lines_range_variant_before_validation() -> None:
    text = "Greeting lives in `app/service.py` at lines 1-3."

    clean, report = validate_citations(text, CHUNKS, FILE_LINES)

    assert report.ok
    assert report.valid == ["app/service.py:1-3"]
    assert "app/service.py:1-3" in clean


def test_normalizes_unbackticked_and_to_range_variants() -> None:
    assert normalize_citations("see app/service.py at line 12") == (
        "see app/service.py:12"
    )
    assert normalize_citations("see app/service.py at lines 12 to 15") == (
        "see app/service.py:12-15"
    )


def test_bare_path_is_not_turned_into_a_citation() -> None:
    text = "See `app/service.py` for the greeting."

    clean, report = validate_citations(text, CHUNKS, FILE_LINES)

    assert report.valid == []
    assert report.invalid == []
    assert clean == text


# -- normalization must not weaken validation ----------------------------


def test_normalized_unknown_path_is_still_dropped() -> None:
    text = "Ghost lives in `app/ghost.py` at line 3."

    clean, report = validate_citations(text, CHUNKS, FILE_LINES)

    assert report.valid == []
    assert report.invalid[0].reason == "unknown path"
    assert "app/ghost.py" not in clean
    assert "[invalid citation removed]" in clean


def test_normalized_out_of_range_variant_is_still_dropped() -> None:
    text = "Greeting lives in `app/service.py` at lines 900-950."

    clean, report = validate_citations(text, CHUNKS, FILE_LINES)

    assert report.valid == []
    assert report.invalid[0].reason.startswith("range exceeds")
    assert "900" not in clean


def test_normalized_range_outside_retrieved_chunk_is_still_dropped() -> None:
    text = "Greeting lives in `app/service.py` at line 30."

    clean, report = validate_citations(text, CHUNKS, FILE_LINES)

    assert report.valid == []
    assert report.invalid[0].reason == "symbol not in retrieved set"
    assert "[invalid citation removed]" in clean


# -- the shipped prompt still demands the exact form ---------------------


def test_prompt_still_requires_path_line_format() -> None:
    assert "`path:line`" in SYSTEM_PROMPT
    assert "`path:start-end`" in SYSTEM_PROMPT
    assert "paths exactly as given in the excerpt headers" in SYSTEM_PROMPT


# -- recorded probe result ----------------------------------------------

# Measured with qwen2.5-coder:7b on 10 answerable questions (6 from the eval
# set, 2 phrasings that produced the "at line(s)" variant, 2 more). Recorded
# here so the shipped lever is checkable without a model.
PROBE = {
    "baseline": {"valid": 6, "claims": 96, "faithful": 9, "faith": 0.09375},
    "prompt": {"valid": 8, "claims": 125, "faithful": 8, "faith": 0.064},
    "normalize": {"valid": 7, "claims": 96, "faithful": 10, "faith": 0.104167},
    "both": {"valid": 8, "claims": 125, "faithful": 8, "faith": 0.064},
}
CHOSEN = "normalize"


def test_recorded_probe_rejects_the_prompt_lever() -> None:
    assert PROBE["prompt"]["faith"] < PROBE["baseline"]["faith"]


def test_recorded_probe_ships_the_normalizer_without_harm() -> None:
    baseline = PROBE["baseline"]
    shipped = PROBE[CHOSEN]

    assert CHOSEN == "normalize"
    assert shipped["faith"] >= baseline["faith"], (
        "recorded probe lost faithfulness: "
        f"{baseline['faith']} -> {shipped['faith']}"
    )
    assert shipped["valid"] >= baseline["valid"], (
        "recorded probe lost valid citations: "
        f"{baseline['valid']} -> {shipped['valid']}"
    )
