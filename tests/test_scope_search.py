"""File-scoped explaining and model-free search.

These pin the two additions to the web surface: an Ask may be limited to one
indexed file, and ``/api/search`` returns retrieval hits without touching the
model. The browser cache key is checked as a static contract because the cache
itself lives in sessionStorage.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.answer import REFUSAL, SCOPED_SYSTEM_PROMPT, SYSTEM_PROMPT, answer
from atlas.entrypoints import find_entrypoints
from atlas.graph import build_graph, open_graph
from atlas.manifest import build_manifest, write_manifest
from atlas.narrate import ModelInfo
from atlas.retrieval import retrieve
from atlas.walker import scan
from atlas.web import STATIC_DIR, create_app


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    write(tmp_path, "app/main.py", "from app.service import greet\n\n\ndef main():\n    greet('world')\n")
    write(
        tmp_path,
        "app/service.py",
        "from util.text import shout\n\n\n"
        "def greet(name):\n"
        '    """Say hello to a person."""\n'
        '    return shout("hi " + name)\n\n\n'
        "def helper(name):\n"
        "    return name\n",
    )
    write(tmp_path, "util/text.py", "def shout(text):\n    return text.upper()\n")
    write(tmp_path, "README.md", "# demo\n\nA tiny repo.\n")

    scanned = scan(tmp_path)
    build_graph(tmp_path, scanned)
    manifest = build_manifest(scanned, find_entrypoints(tmp_path, scanned.files))
    write_manifest(tmp_path, manifest)
    return tmp_path


@pytest.fixture()
def client(repo: Path) -> TestClient:
    return TestClient(create_app(repo))


# -- scoped retrieval --------------------------------------------------


def test_scoped_retrieval_returns_only_that_file(repo: Path) -> None:
    conn = open_graph(repo)
    try:
        result = retrieve(conn, "greet shout", scope="app/service.py")
    finally:
        conn.close()

    assert result.chunks
    assert {chunk.path for chunk in result.chunks} == {"app/service.py"}
    assert result.refused is False


def test_scoped_retrieval_falls_back_to_the_file_when_nothing_matches(repo: Path) -> None:
    conn = open_graph(repo)
    try:
        result = retrieve(conn, "kubernetes ingress", scope="app/service.py")
    finally:
        conn.close()

    # A weak lexical score is meaningless when the file itself is the context,
    # so the file's chunks are used in reading order rather than refusing.
    assert result.refused is False
    assert result.chunks
    assert {chunk.path for chunk in result.chunks} == {"app/service.py"}
    starts = [chunk.start for chunk in result.chunks]
    assert starts == sorted(starts)


def test_scoped_retrieval_refuses_unindexed_file(repo: Path) -> None:
    conn = open_graph(repo)
    try:
        result = retrieve(conn, "greet", scope="missing/nope.py")
    finally:
        conn.close()

    assert result.refused is True
    assert result.chunks == []
    assert "no indexed chunks" in result.reason


# -- scoped ask --------------------------------------------------------


def test_scoped_ask_limits_prompt_to_the_file(client: TestClient, monkeypatch) -> None:
    info = ModelInfo("fake", "fake-model", "http://127.0.0.1:9")
    monkeypatch.setattr("atlas.web.detect_model", lambda timeout=None: info)
    prompts: list[str] = []

    def fake_complete(_system, prompt, _info):
        prompts.append(prompt)
        return "`greet` calls `shout` (`app/service.py:3-5`)."

    monkeypatch.setattr("atlas.answer.complete", fake_complete)

    payload = client.post(
        "/api/ask",
        json={"question": "how does greet work?", "path": "app/service.py"},
    ).json()

    assert payload["mode"] == "answer"
    assert payload["scope"] == "app/service.py"
    assert payload["trusted"] is True
    assert payload["citations"]["valid"] == ["app/service.py:3-5"]
    assert {chunk["path"] for chunk in payload["retrieval"]["chunks"]} == {"app/service.py"}
    assert len(prompts) == 1
    assert "app/service.py" in prompts[0]
    assert "util/text.py" not in prompts[0]


def test_scoped_answer_uses_the_overview_contract(repo: Path) -> None:
    conn = open_graph(repo)
    try:
        retrieval = retrieve(conn, "Explain this file.", scope="app/service.py")
    finally:
        conn.close()

    seen: dict[str, str] = {}

    def narrator(system: str, prompt: str) -> str:
        seen["system"] = system
        seen["prompt"] = prompt
        return "`greet` calls `shout` (`app/service.py:3-5`)."

    result = answer(
        "Explain this file.",
        retrieval,
        {"app/service.py": 9},
        narrator=narrator,
        scope="app/service.py",
    )

    assert result.trusted is True
    assert seen["system"] == SCOPED_SYSTEM_PROMPT
    assert "3 to 5 bullets" in seen["system"]
    assert "never exceed 220" in seen["system"]
    assert "short file overview" in seen["prompt"]


def test_repo_wide_answer_keeps_the_standard_contract(repo: Path) -> None:
    conn = open_graph(repo)
    try:
        retrieval = retrieve(conn, "how does greet work?")
    finally:
        conn.close()

    seen: dict[str, str] = {}

    def narrator(system: str, prompt: str) -> str:
        seen["system"] = system
        seen["prompt"] = prompt
        return "`greet` calls `shout` (`app/service.py:3-5`)."

    answer(
        "how does greet work?",
        retrieval,
        {"app/service.py": 9},
        narrator=narrator,
    )

    assert seen["system"] == SYSTEM_PROMPT
    assert "file overview request" not in seen["system"]
    assert "short file overview" not in seen["prompt"]
    assert "Answer the question" in seen["prompt"]


def test_scoped_ask_refuses_unindexed_file(client: TestClient, monkeypatch) -> None:
    def no_model(timeout=None):
        raise AssertionError("an unindexed scope must refuse before the model")

    monkeypatch.setattr("atlas.web.detect_model", no_model)

    payload = client.post(
        "/api/ask", json={"question": "what is this?", "path": "missing/nope.py"}
    ).json()

    assert payload["mode"] == "refused"
    assert payload["answer"] == REFUSAL
    assert payload["scope"] == "missing/nope.py"
    assert payload["retrieval"] is None


def test_scoped_ask_rejects_traversal(client: TestClient) -> None:
    payload = client.post(
        "/api/ask", json={"question": "what is this?", "path": "../secret.py"}
    ).json()

    assert payload["mode"] == "refused"
    assert payload["scope"] == "../secret.py"
    assert payload["retrieval"] is None


def test_repo_wide_ask_has_no_scope(client: TestClient, monkeypatch) -> None:
    info = ModelInfo("fake", "fake-model", "http://127.0.0.1:9")
    monkeypatch.setattr("atlas.web.detect_model", lambda timeout=None: info)
    monkeypatch.setattr(
        "atlas.answer.complete",
        lambda _system, _prompt, _info: "`greet` calls `shout` (`app/service.py:3-5`).",
    )

    payload = client.post("/api/ask", json={"question": "how does greet work?"}).json()

    assert payload["mode"] == "answer"
    assert payload["scope"] is None


# -- instant search ----------------------------------------------------


def test_search_returns_retrieval_hits_without_a_model(
    client: TestClient, monkeypatch
) -> None:
    def no_model(timeout=None):
        raise AssertionError("/api/search must not call the model")

    monkeypatch.setattr("atlas.web.detect_model", no_model)

    payload = client.get("/api/search", params={"q": "shout"}).json()

    assert payload["query"] == "shout"
    assert payload["items"]
    first = payload["items"][0]
    assert first["path"] == "util/text.py"
    assert isinstance(first["start"], int)
    assert isinstance(first["end"], int)
    assert first["start"] >= 1
    assert first["end"] >= first["start"]
    assert first["snippet"]
    assert payload["elapsed_ms"] >= 0
    # No prompt, no answer, no model metadata can leak into a lookup.
    assert "answer" not in payload
    assert "model" not in payload


def test_search_reports_no_matches(client: TestClient) -> None:
    payload = client.get("/api/search", params={"q": "kubernetes ingress"}).json()

    assert payload["items"] == []
    assert payload["reason"]


def test_search_requires_a_query(client: TestClient) -> None:
    assert client.get("/api/search").status_code == 422
    assert client.get("/api/search", params={"q": ""}).status_code == 422


# -- cache contract ----------------------------------------------------


def test_session_cache_key_includes_scope() -> None:
    source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    # A repo-wide answer and a file-scoped answer for the same question must
    # not collide in sessionStorage.
    assert "function answerCacheKey(question, scope)" in source
    assert '${scope || "repo"}' in source
    assert "JSON.stringify(scope ? { question, path: scope } : { question })" in source
