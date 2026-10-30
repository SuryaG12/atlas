from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlas.answer import REFUSAL, CitationReport, answer, validate_citations
from atlas.cli import app
from atlas.graph import build_graph, open_graph
from atlas.narrate import ModelInfo
from atlas.retrieval import (
    RetrievalChunk,
    extract_tokens,
    expand,
    fts_search,
    fuse,
    retrieve,
    symbol_lookup,
)
from atlas.walker import scan


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    write(
        tmp_path,
        "app/main.py",
        "from app.service import greet\n\n"
        "def main():\n"
        "    greet('world')\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
    )
    write(
        tmp_path,
        "app/service.py",
        'def greet(name):\n'
        '    """Say hello to a person."""\n'
        "    return helper(name)\n\n"
        "def helper(name):\n"
        '    return "hi " + name\n',
    )
    build_graph(tmp_path, scan(tmp_path))
    return tmp_path


@pytest.fixture()
def conn(repo: Path):
    connection = open_graph(repo)
    yield connection
    connection.close()


def chunk_id(conn, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM chunks WHERE name = ? ORDER BY id LIMIT 1", (name,)
    ).fetchone()
    assert row is not None
    return row[0]


def make_chunk(**overrides) -> RetrievalChunk:
    payload = dict(
        chunk_id=1,
        symbol_id=1,
        path="app/service.py",
        name="greet",
        kind="function",
        start=1,
        end=3,
        text="def greet(name):\n    return helper(name)\n",
        docstring="",
        tokens=20,
    )
    payload.update(overrides)
    return RetrievalChunk(**payload)


# -- tokens -------------------------------------------------------------


def test_extract_tokens_splits_identifiers_and_drops_stopwords() -> None:
    tokens = extract_tokens("Where is validateToken called, and how does the repo work?")

    assert "validateToken" in tokens
    assert "validate" in tokens
    assert "Token" in tokens
    assert "where" not in tokens
    assert "repo" not in tokens
    assert "how" not in tokens


def test_extract_tokens_handles_snake_case() -> None:
    tokens = extract_tokens("what calls parse_file and _insert_parsed")
    assert "parse" in tokens
    assert "insert" in tokens
    assert "parsed" in tokens
    assert "_insert_parsed" in tokens


# -- fusion -------------------------------------------------------------


def test_fuse_rewards_agreement() -> None:
    hits = fuse({"symbol": [1, 2, 3], "fts": [1, 4], "graph": [2]})
    order = [chunk_id for chunk_id, _score, _ranks in hits]
    scores = {chunk_id: score for chunk_id, score, _ranks in hits}

    assert order[0] == 1
    assert scores[1] > scores[2]
    assert hits[0][2] == {"symbol": 1, "fts": 1}


def test_fuse_is_deterministic_on_ties() -> None:
    first = fuse({"symbol": [5, 6], "fts": [6, 5]})
    second = fuse({"symbol": [5, 6], "fts": [6, 5]})
    assert first == second


# -- retrieval sources --------------------------------------------------


def test_symbol_lookup_finds_exact_match(conn) -> None:
    ids, exact = symbol_lookup(conn, ["greet"])

    assert ids
    assert exact
    top = conn.execute("SELECT name FROM chunks WHERE id=?", (ids[0],)).fetchone()
    assert top["name"] == "greet"


def test_fts_search_reads_docstrings(conn) -> None:
    ids, raw = fts_search(conn, ["hello"])

    names = {
        row["name"]
        for row in conn.execute(
            f"SELECT name FROM chunks WHERE id IN ({','.join('?' for _ in ids)})", ids
        )
    }
    assert "greet" in names
    assert raw


def test_expansion_adds_callers_and_callees(conn) -> None:
    seed = chunk_id(conn, "greet")
    expanded = expand(conn, [seed])

    names = {
        row["name"]
        for row in conn.execute(
            f"SELECT name FROM chunks WHERE id IN ({','.join('?' for _ in expanded)})",
            expanded,
        )
    }
    assert "helper" in names  # callee
    assert "main" in names  # caller


def test_expansion_handles_inheritance(tmp_path: Path) -> None:
    write(
        tmp_path,
        "shapes.py",
        "class Base:\n    pass\n\nclass Circle(Base):\n    pass\n",
    )
    build_graph(tmp_path, scan(tmp_path))
    connection = open_graph(tmp_path)
    try:
        base = chunk_id(connection, "Base")
        expanded = expand(connection, [base])
        names = {
            row["name"]
            for row in connection.execute(
                f"SELECT name FROM chunks WHERE id IN ({','.join('?' for _ in expanded)})",
                expanded,
            )
        }
    finally:
        connection.close()

    assert "Circle" in names


# -- end-to-end retrieval ----------------------------------------------


def test_retrieve_real_question(conn) -> None:
    result = retrieve(conn, "how does greet work?")

    assert not result.refused
    assert result.chunks
    assert result.chunks[0].name == "greet"
    assert "symbol" in result.chunks[0].ranks or "fts" in result.chunks[0].ranks
    assert result.budget_used <= 6000
    assert result.sources["symbol"] or result.sources["fts"]


def test_retrieve_refuses_unknown_question(conn) -> None:
    result = retrieve(conn, "how does the kubernetes ingress shard traffic?")

    assert result.refused
    assert result.chunks == []
    assert "no symbol" in result.reason or "score" in result.reason


def test_retrieve_respects_top_k_and_budget(conn) -> None:
    result = retrieve(conn, "greet helper main", top_k=1, budget=400)

    assert len(result.chunks) == 1
    assert result.budget_used <= 400


def test_retrieve_respects_min_score(conn) -> None:
    result = retrieve(conn, "hello person", min_score=0.9)
    assert result.refused
    assert "score" in result.reason


# -- citation validation -----------------------------------------------


FILE_LINES = {"app/service.py": 50, "app/main.py": 10}


def test_validation_accepts_real_citation() -> None:
    chunks = [make_chunk()]
    text = "Greeting lives in `app/service.py:1-3`."

    clean, report = validate_citations(text, chunks, FILE_LINES)

    assert report.ok
    assert report.valid == ["app/service.py:1-3"]
    assert "app/service.py:1-3" in clean


def test_validation_catches_fake_line_number() -> None:
    chunks = [make_chunk()]
    text = "Greeting lives in `app/service.py:900-950`."

    clean, report = validate_citations(text, chunks, FILE_LINES)

    assert not report.ok
    assert report.invalid[0].reason.startswith("range exceeds")
    assert "900" not in clean


def test_validation_catches_citation_outside_retrieved_set() -> None:
    chunks = [make_chunk()]
    text = "Main starts at `app/main.py:1-2`."

    _clean, report = validate_citations(text, chunks, FILE_LINES)

    assert not report.ok
    assert report.invalid[0].reason == "symbol not in retrieved set"


def test_validation_catches_unknown_path() -> None:
    chunks = [make_chunk()]
    text = "See `app/ghost.py:1`."

    _clean, report = validate_citations(text, chunks, FILE_LINES)

    assert not report.ok
    assert report.invalid[0].reason == "unknown path"


def test_validation_keeps_good_and_strips_bad() -> None:
    chunks = [make_chunk()]
    text = "Good `app/service.py:1-2`, bad `app/service.py:500`."

    clean, report = validate_citations(text, chunks, FILE_LINES)

    assert report.valid == ["app/service.py:1-2"]
    assert len(report.invalid) == 1
    assert "500" not in clean


# -- answer assembly ----------------------------------------------------


def test_answer_with_narrator_produces_trusted_output(conn) -> None:
    retrieval = retrieve(conn, "how does greet work?")
    file_lines = {"app/service.py": 6, "app/main.py": 8}

    def narrator(system: str, prompt: str) -> str:
        assert "greet" in prompt
        return "`greet` calls `helper` (`app/service.py:1-3`)."

    result = answer("how does greet work?", retrieval, file_lines, narrator=narrator)

    assert not result.refused
    assert result.trusted
    assert result.citations.valid
    assert "app/service.py:1-3" in result.answer


def test_answer_flags_invalid_citation_from_model(conn) -> None:
    retrieval = retrieve(conn, "how does greet work?")
    file_lines = {"app/service.py": 6, "app/main.py": 8}

    def narrator(_system: str, _prompt: str) -> str:
        return "Greet calls helper (`app/service.py:1-3`) and ghosts (`app/service.py:900`)."

    result = answer("how does greet work?", retrieval, file_lines, narrator=narrator)

    assert result.citations.valid
    assert len(result.citations.invalid) == 1
    assert "900" not in result.answer
    assert not result.trusted


def test_answer_passes_through_model_refusal(conn) -> None:
    retrieval = retrieve(conn, "how does greet work?")

    result = answer(
        "how does greet work?",
        retrieval,
        {"app/service.py": 6},
        narrator=lambda _s, _p: REFUSAL,
    )

    assert result.refused
    assert result.answer == REFUSAL


def test_answer_refuses_before_calling_model(conn) -> None:
    retrieval = retrieve(conn, "kubernetes ingress sharding")
    called = False

    def narrator(_system: str, _prompt: str) -> str:
        nonlocal called
        called = True
        return "should not run"

    result = answer("kubernetes ingress sharding", retrieval, {}, narrator=narrator)

    assert result.refused
    assert result.answer == REFUSAL
    assert called is False


# -- CLI ----------------------------------------------------------------


runner = CliRunner()


def test_cli_no_llm_json(repo: Path) -> None:
    result = runner.invoke(
        app, ["ask", "how does greet work?", "--path", str(repo), "--no-llm", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "no-llm"
    assert payload["chunks"]
    assert payload["fused"]
    assert {"symbol", "fts", "graph"} <= set(payload["sources"])


def test_cli_no_llm_text(repo: Path) -> None:
    result = runner.invoke(
        app, ["ask", "how does greet work?", "--path", str(repo), "--no-llm"]
    )

    assert result.exit_code == 0
    assert "Chunks" in result.stdout
    assert "Sources" in result.stdout


def test_cli_refusal(repo: Path) -> None:
    result = runner.invoke(
        app, ["ask", "kubernetes ingress sharding", "--path", str(repo), "--no-llm"]
    )

    assert result.exit_code == 0
    assert "couldn't find that in this repo" in result.stdout.lower()


def test_cli_reports_missing_model(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr("atlas.cli.detect_model", lambda: None)
    result = runner.invoke(app, ["ask", "how does greet work?", "--path", str(repo)])

    assert result.exit_code == 2
    assert "No local model configured" in result.output


def test_cli_full_answer_with_fake_model(repo: Path, monkeypatch) -> None:
    info = ModelInfo("fake", "fake-model", "http://127.0.0.1:9")
    monkeypatch.setattr("atlas.cli.detect_model", lambda: info)
    monkeypatch.setattr(
        "atlas.answer.complete",
        lambda _system, _prompt, _info: "`greet` calls `helper` (`app/service.py:1-3`).",
    )

    result = runner.invoke(
        app, ["ask", "how does greet work?", "--path", str(repo), "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["trusted"] is True
    assert payload["citations"]["valid"] == ["app/service.py:1-3"]
    assert payload["model"]["model"] == "fake-model"


def test_cli_catches_fake_citation(repo: Path, monkeypatch) -> None:
    info = ModelInfo("fake", "fake-model", "http://127.0.0.1:9")
    monkeypatch.setattr("atlas.cli.detect_model", lambda: info)
    monkeypatch.setattr(
        "atlas.answer.complete",
        lambda _system, _prompt, _info: "Ghost (`app/service.py:999-1000`).",
    )

    result = runner.invoke(
        app, ["ask", "how does greet work?", "--path", str(repo), "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["citations"]["invalid"]
    assert payload["citations"]["valid"] == []
    assert "999" not in payload["answer"]
