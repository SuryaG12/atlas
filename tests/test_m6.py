"""Refusal-gate sweep: measured thresholds, real retrieval, no new scorer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlas.cli import app
from atlas.eval import (
    MIN_SCORE,
    SWEEP_THRESHOLDS,
    SWEEP_TARGET_RECALL,
    SWEEP_TARGET_REFUSAL,
    Question,
    RepoSpec,
    render_markdown,
    run_eval,
)
from atlas.graph import build_graph
from atlas.walker import scan

RUNNER = CliRunner()


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    write(
        root,
        "app/service.py",
        "def greet(name):\n"
        '    """Say hello to a person."""\n'
        "    return format_name(name)\n"
        "\n"
        "def format_name(name):\n"
        '    """Normalize a display name by trimming and title-casing it."""\n'
        "    return name.strip().title()\n",
    )
    build_graph(root, scan(root))
    return root


def specs(repo: Path) -> dict[str, RepoSpec]:
    return {"repo": RepoSpec("repo", repo, str(repo))}


def answerable() -> Question:
    return Question(
        id="q-answer",
        question="How is format_name used to normalize a display name?",
        repo="repo",
        answerable=True,
        expected_files=["app/service.py"],
        expected_symbols=["format_name"],
        expected_citations=["app/service.py:5-7"],
    )


def refused_question() -> Question:
    return Question(
        id="q-refused",
        question="Where is the Kubernetes ingress sharding logic implemented?",
        repo="repo",
        answerable=False,
    )


def accepted_question() -> Question:
    """No exact symbol name, but enough incidental overlap to clear 0.02."""
    return Question(
        id="q-accepted",
        question="Where is the display name normalization for Kubernetes pods?",
        repo="repo",
        answerable=False,
    )


def test_sweep_rows_match_a_real_run_at_every_threshold(repo: Path) -> None:
    questions = [answerable(), refused_question()]
    report = run_eval(questions, specs(repo), sweep=[0.02, 0.5])

    assert [row["threshold"] for row in report.sweep] == [0.02, 0.5]
    for row in report.sweep:
        baseline = run_eval(
            questions, specs(repo), min_score=row["threshold"]
        ).metrics["overall"]
        assert row["recall_at_5"] == baseline["recall_at_5"]
        assert row["refusal_accuracy"] == baseline["refusal_accuracy"]
        assert row["recall_hits"] == baseline["recall_hits"]
        assert row["refusals_correct"] == baseline["refusals_correct"]


def test_score_only_gate_removes_the_exact_symbol_bypass(repo: Path) -> None:
    report = run_eval(
        [answerable(), refused_question()], specs(repo), sweep=[0.5]
    )
    row = report.sweep[0]

    # `format_name` is an exact symbol name, so the shipped gate keeps it even
    # though its fused score is far below 0.5.
    assert row["recall_at_5"] == 1.0
    assert row["answerable_refusals"] == 0
    # A plain score cut has no bypass and throws the same answer away.
    assert row["score_only_recall_at_5"] == 0.0
    # Refusal on the unanswerable question is unchanged by the bypass.
    assert row["refusal_accuracy"] == 1.0
    assert row["score_only_refusal_accuracy"] == 1.0


def test_sweep_marks_rows_that_meet_both_floors(repo: Path) -> None:
    report = run_eval(
        [answerable(), refused_question()], specs(repo), sweep=[0.02, 0.5]
    )
    text = render_markdown(report)

    assert "## Threshold sweep" in text
    assert "Recall@5 (exact gate)" in text
    assert "Refusal (score only)" in text
    assert f"{SWEEP_TARGET_RECALL:.3f}" in text
    assert f"{SWEEP_TARGET_REFUSAL:.3f}" in text
    assert "Thresholds meeting both floors" in text


def test_unanswerable_questions_the_gate_still_accepts_are_listed(
    repo: Path,
) -> None:
    report = run_eval(
        [answerable(), accepted_question()], specs(repo), sweep=None
    )
    outcome = report.outcomes[1]

    assert outcome.answerable is False
    assert outcome.best_score >= MIN_SCORE
    assert outcome.refused is False

    text = render_markdown(report)
    assert "## Refusal cases" in text
    assert f"`{outcome.id}`" in text
    assert "**false positive**" in text


def test_answerable_recall_survives_the_sweep(repo: Path) -> None:
    report = run_eval(
        [answerable(), refused_question()],
        specs(repo),
        sweep=list(SWEEP_THRESHOLDS),
    )

    for row in report.sweep:
        if row["threshold"] <= 0.02:
            assert row["recall_at_5"] == 1.0
            assert row["answerable_refusals"] == 0
    assert report.metrics["overall"]["recall_at_5"] == 1.0


def test_cli_sweep_flag_adds_the_table(repo: Path, tmp_path: Path) -> None:
    question_file = tmp_path / "questions.json"
    question_file.write_text(
        '[{"id": "q-answer", "question": "How is format_name used to'
        ' normalize a display name?", "repo": "repo", "answerable": true,'
        ' "expected_files": ["app/service.py"],'
        ' "expected_symbols": ["format_name"],'
        ' "expected_citations": ["app/service.py:5-7"]},'
        ' {"id": "q-refused", "question": "Where is the Kubernetes ingress'
        ' sharding logic implemented?", "repo": "repo", "answerable": false,'
        ' "expected_files": [], "expected_symbols": [],'
        ' "expected_citations": []}]',
        encoding="utf-8",
    )
    (tmp_path / "repos.json").write_text(
        json.dumps({"repo": {"path": str(repo)}}), encoding="utf-8"
    )

    result = RUNNER.invoke(
        app,
        ["eval", "--questions", str(question_file), "--no-narration", "--sweep"],
    )

    assert result.exit_code == 0, result.output
    assert "## Threshold sweep" in result.output
    assert "0.0200" in result.output
    assert "0.1000" in result.output
