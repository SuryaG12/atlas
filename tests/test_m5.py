from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlas.answer import CitationIssue, CitationReport
from atlas.cli import app
from atlas.eval import (
    EvalError,
    Question,
    RepoSpec,
    citation_validity,
    load_questions,
    resolve_repos,
    run_eval,
    score_faithfulness,
    score_retrieval,
    split_claims,
)
from atlas.graph import build_graph, open_graph
from atlas.retrieval import RetrievalChunk, RetrievalResult, retrieve
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


def make_chunk(**overrides) -> RetrievalChunk:
    payload = dict(
        chunk_id=1,
        symbol_id=1,
        path="app/service.py",
        name="format_name",
        kind="function",
        start=5,
        end=7,
        text="",
        docstring="",
        tokens=10,
    )
    payload.update(overrides)
    return RetrievalChunk(**payload)


def retrieval(*chunks: RetrievalChunk) -> RetrievalResult:
    return RetrievalResult(question="q", tokens=[], chunks=list(chunks))


def answerable(repo_path: Path) -> Question:
    return Question(
        id="q-answer",
        question="How is a display name normalized?",
        repo="repo",
        answerable=True,
        expected_files=["app/service.py"],
        expected_symbols=["format_name"],
        expected_citations=["app/service.py:5-7"],
    )


def unanswerable(repo_path: Path) -> Question:
    return Question(
        id="q-missing",
        question="Where is the Kubernetes ingress sharding logic implemented?",
        repo="repo",
        answerable=False,
    )


def write_question_file(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "eval" / "questions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def entry(question_id: str, repo_path: Path, **overrides) -> dict:
    payload = {
        "id": question_id,
        "question": "How is a display name normalized?",
        "repo": str(repo_path),
        "answerable": True,
        "expected_files": ["app/service.py"],
        "expected_symbols": ["format_name"],
        "expected_citations": ["app/service.py:5-7"],
    }
    payload.update(overrides)
    return payload


# -- synthetic metric calculations -----------------------------------------


def test_recall_at_five_uses_files_symbols_and_top_five() -> None:
    question = answerable(Path("repo"))
    hit_file = make_chunk(path="app/service.py", name="other")
    hit_symbol = make_chunk(name="Service.format_name")
    miss = make_chunk(path="app/other.py", name="unrelated")

    assert score_retrieval(question, retrieval(hit_file))["recall_at_5"] is True
    assert score_retrieval(question, retrieval(hit_symbol))["recall_at_5"] is True
    assert score_retrieval(question, retrieval(miss))["recall_at_5"] is False

    four_misses = [make_chunk(chunk_id=i, path="app/other.py", name="x") for i in range(4)]
    fifth = make_chunk(chunk_id=9, path="app/service.py", name="other")
    assert score_retrieval(question, retrieval(*four_misses, fifth))["recall_at_5"] is True

    five_misses = [make_chunk(chunk_id=i, path="app/other.py", name="x") for i in range(5)]
    assert score_retrieval(question, retrieval(*five_misses, fifth))["recall_at_5"] is False


def test_citation_validity_is_valid_over_total_and_none_when_empty() -> None:
    report = CitationReport(
        valid=["app/service.py:5"],
        invalid=[CitationIssue(citation="app/nope.py:1", reason="unknown path")],
    )

    assert citation_validity([report]) == 0.5
    assert citation_validity([]) is None
    assert citation_validity([CitationReport()]) is None


def test_faithfulness_fails_uncited_claims() -> None:
    chunk = make_chunk(start=5, end=7)
    text = (
        "The helper trims and title-cases the name `app/service.py:5-7`.\n"
        "It also returns a string."
    )

    claims = split_claims(text)
    score = score_faithfulness(text, [chunk], {"app/service.py": 7})

    assert len(claims) == 2
    assert score["claims"] == 2
    assert score["faithful_claims"] == 1
    assert score["faithfulness"] == 0.5
    assert score["uncited_claims"] == ["It also returns a string."]


def test_faithfulness_rejects_a_citation_outside_the_retrieved_chunks() -> None:
    chunk = make_chunk(start=5, end=7, path="app/service.py")
    text = "The helper is elsewhere `app/other.py:1`."

    score = score_faithfulness(
        text, [chunk], {"app/service.py": 7, "app/other.py": 7}
    )

    assert score["claims"] == 1
    assert score["faithful_claims"] == 0
    assert score["unsupported_claims"] == [text]


# -- question file validation ----------------------------------------------


def test_missing_question_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="Question file not found"):
        load_questions(tmp_path / "missing.json")


def test_duplicate_question_ids_are_rejected(tmp_path: Path) -> None:
    path = write_question_file(
        tmp_path,
        [entry("same-id", tmp_path), entry("same-id", tmp_path)],
    )

    with pytest.raises(EvalError, match="Duplicate question id: same-id"):
        load_questions(path)


def test_unanswerable_question_cannot_declare_ground_truth(tmp_path: Path) -> None:
    path = write_question_file(
        tmp_path,
        [
            entry(
                "bad",
                tmp_path,
                answerable=False,
                expected_files=[],
                expected_symbols=["format_name"],
                expected_citations=[],
            )
        ],
    )

    with pytest.raises(EvalError, match="must not declare expected"):
        load_questions(path)


def test_answerable_question_requires_ground_truth(tmp_path: Path) -> None:
    path = write_question_file(
        tmp_path,
        [
            entry(
                "bad",
                tmp_path,
                expected_files=[],
                expected_symbols=[],
                expected_citations=[],
            )
        ],
    )

    with pytest.raises(EvalError, match="needs at least one expected"):
        load_questions(path)


def test_unverifiable_symbol_aborts_the_run(repo: Path) -> None:
    question = Question(
        id="bad-symbol",
        question="Where is the display name normalized?",
        repo="repo",
        answerable=True,
        expected_files=["app/service.py"],
        expected_symbols=["does_not_exist"],
        expected_citations=["app/service.py:5-7"],
    )
    specs = {"repo": RepoSpec("repo", repo, str(repo))}

    with pytest.raises(EvalError, match="not in the index"):
        run_eval([question], specs)


def test_unverifiable_citation_aborts_the_run(repo: Path) -> None:
    question = Question(
        id="bad-citation",
        question="Where is the display name normalized?",
        repo="repo",
        answerable=True,
        expected_files=["app/service.py"],
        expected_symbols=["format_name"],
        expected_citations=["app/service.py:1-99"],
    )
    specs = {"repo": RepoSpec("repo", repo, str(repo))}

    with pytest.raises(EvalError, match="exceeds the file length"):
        run_eval([question], specs)


# -- end-to-end synthetic run ----------------------------------------------


def test_unanswerable_questions_refuse_under_the_m2_rule(repo: Path) -> None:
    question = unanswerable(repo)
    conn = open_graph(repo)
    try:
        result = retrieve(conn, question.question)
    finally:
        conn.close()

    assert result.refused is True
    assert result.best_score < 0.02
    assert result.exact_symbol_match is False

    report = run_eval([question], {"repo": RepoSpec("repo", repo, str(repo))})
    outcome = report.outcomes[0]
    assert outcome.refused is True
    assert report.metrics["overall"]["refusal_accuracy"] == 1.0


def test_run_eval_scores_recall_and_reports_no_narration(repo: Path) -> None:
    questions = [answerable(repo), unanswerable(repo)]
    report = run_eval(
        questions, {"repo": RepoSpec("repo", repo, str(repo))}, model=None
    )
    metrics = report.metrics["overall"]

    assert metrics["answerable"] == 1
    assert metrics["unanswerable"] == 1
    assert metrics["recall_at_5"] == 1.0
    assert metrics["refusal_accuracy"] == 1.0
    assert metrics["citation_validity"] is None
    assert metrics["faithfulness"] is None
    assert report.narration["status"] == "not evaluated"
    assert report.narration["citation_validity"] is None


def test_run_eval_is_deterministic(repo: Path) -> None:
    questions = [answerable(repo), unanswerable(repo)]
    specs = {"repo": RepoSpec("repo", repo, str(repo))}

    first = run_eval(questions, specs, model=None)
    second = run_eval(questions, specs, model=None)

    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )


def test_resolve_repos_reads_relative_paths(tmp_path: Path, repo: Path) -> None:
    question = answerable(tmp_path)
    question.repo = "repo"
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    repos_file = eval_dir / "repos.json"
    repos_file.write_text(
        json.dumps({"repo": {"path": str(repo), "tag": "test", "commit": "abc"}}),
        encoding="utf-8",
    )

    specs = resolve_repos([question], repos_file)

    assert specs["repo"].path == repo
    assert specs["repo"].tag == "test"


# -- CLI --------------------------------------------------------------------


def test_cli_eval_json_and_out_file(tmp_path: Path, repo: Path) -> None:
    question_file = write_question_file(
        tmp_path, [entry("q-answer", repo), entry("q-missing", repo)]
    )
    # The second entry is answerable in the helper; make it unanswerable.
    payload = json.loads(question_file.read_text(encoding="utf-8"))
    payload[1].update(
        {
            "answerable": False,
            "expected_files": [],
            "expected_symbols": [],
            "expected_citations": [],
            "question": "Where is the Kubernetes ingress sharding logic implemented?",
        }
    )
    question_file.write_text(json.dumps(payload), encoding="utf-8")

    result = RUNNER.invoke(
        app,
        ["eval", "--questions", str(question_file), "--json", "--no-narration"],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["metrics"]["overall"]["recall_at_5"] == 1.0
    assert data["narration"]["status"] == "not evaluated"

    out = tmp_path / "EVAL.md"
    result = RUNNER.invoke(
        app,
        [
            "eval",
            "--questions",
            str(question_file),
            "--no-narration",
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    text = out.read_text(encoding="utf-8")
    assert "# Atlas M5 evaluation" in text
    assert "Narration: not evaluated" in text


def test_cli_eval_rejects_a_bad_question_file(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app, ["eval", "--questions", str(tmp_path / "missing.json")]
    )

    assert result.exit_code == 2
    assert "Question file not found" in result.output
