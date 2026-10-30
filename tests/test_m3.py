from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from atlas.answer import REFUSAL
from atlas.cli import app as cli_app
from atlas.entrypoints import find_entrypoints
from atlas.graph import build_graph, open_graph
from atlas.manifest import build_manifest, write_manifest
from atlas.narrate import NO_MODEL_MESSAGE, ModelInfo
from atlas.walker import scan
from atlas.web import BIND_HOST, create_app


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


def indexed_paths(repo: Path) -> list[str]:
    conn = open_graph(repo)
    try:
        return [row[0] for row in conn.execute("SELECT path FROM files ORDER BY path")]
    finally:
        conn.close()


# -- page and static assets --------------------------------------------


def test_page_serves(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Atlas" in response.text
    assert "/static/app.js" in response.text
    assert "/static/vendor/mermaid.min.js" in response.text
    # Local-only surface: no remote assets and no 0.0.0.0 reference.
    assert "http://" not in response.text
    assert "https://" not in response.text
    assert "0.0.0.0" not in response.text


def test_static_assets_served(client: TestClient) -> None:
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/vendor/mermaid.min.js").status_code == 200


def test_bind_host_is_loopback_only() -> None:
    assert BIND_HOST == "127.0.0.1"


# -- meta --------------------------------------------------------------


def test_meta_tree_matches_index(client: TestClient, repo: Path, monkeypatch) -> None:
    monkeypatch.setattr("atlas.web.detect_model", lambda timeout=None: None)

    payload = client.get("/api/meta").json()

    assert sorted(entry["path"] for entry in payload["tree"]) == indexed_paths(repo)
    assert payload["files"] == len(indexed_paths(repo))
    assert payload["symbols"] >= 4
    assert payload["edges"] >= 2
    assert 0.0 <= payload["resolved_pct"] <= 100.0
    assert payload["bind_host"] == "127.0.0.1"
    assert payload["model"] is None
    assert payload["no_model_message"] == NO_MODEL_MESSAGE


def test_meta_reports_model_when_present(client: TestClient, monkeypatch) -> None:
    info = ModelInfo("ollama", "qwen2.5-coder", "http://127.0.0.1:11434")
    monkeypatch.setattr("atlas.web.detect_model", lambda timeout=None: info)

    payload = client.get("/api/meta").json()

    assert payload["model"]["model"] == "qwen2.5-coder"
    assert payload["model"]["provider"] == "ollama"


def test_meta_without_graph_is_plain_409(tmp_path: Path) -> None:
    response = TestClient(create_app(tmp_path)).get("/api/meta")

    assert response.status_code == 409
    assert response.json()["detail"] == "No graph found. Run `atlas index .` first."


# -- file --------------------------------------------------------------


def test_file_endpoint_returns_numbered_source(client: TestClient) -> None:
    payload = client.get("/api/file", params={"path": "app/service.py"}).json()

    assert payload["path"] == "app/service.py"
    assert payload["language"] == "Python"
    assert payload["lines"][0] == "from util.text import shout"
    assert payload["lines"][3] == "def greet(name):"
    assert payload["total_lines"] == len(payload["lines"]) >= 10
    assert payload["truncated"] is False


def test_file_endpoint_rejects_unindexed_and_traversal(client: TestClient, repo: Path) -> None:
    assert client.get("/api/file", params={"path": "ghost.py"}).status_code == 404
    assert client.get("/api/file", params={"path": "../secret.py"}).status_code == 404
    assert client.get("/api/file", params={"path": "../../etc/passwd"}).status_code == 404
    assert client.get("/api/file", params={"path": str(repo / "app" / "main.py")}).status_code == 404


def test_file_endpoint_never_writes(client: TestClient) -> None:
    response = client.post("/api/file", params={"path": "app/service.py"})

    assert response.status_code == 405


# -- map ---------------------------------------------------------------


def test_map_endpoint_is_mermaid_at_file_level(client: TestClient) -> None:
    payload = client.get("/api/map", params={"level": "file"}).json()

    assert payload["level"] == "file"
    assert payload["mermaid"].startswith("graph LR")
    assert any(node["key"] == "app/service.py" for node in payload["nodes"])
    assert any(edge["src"] == "app/main.py" for edge in payload["edges"])


def test_map_endpoint_supports_package_level(client: TestClient) -> None:
    payload = client.get("/api/map", params={"level": "package"}).json()

    assert payload["level"] == "package"
    keys = {node["key"] for node in payload["nodes"]}
    assert {"app", "util"} <= keys
    assert any(edge["src"] == "app" and edge["dst"] == "util" for edge in payload["edges"])


def test_map_rejects_unknown_level(client: TestClient) -> None:
    assert client.get("/api/map", params={"level": "modules"}).status_code == 422


# -- ask ---------------------------------------------------------------


def test_ask_refuses_fake_question(client: TestClient) -> None:
    response = client.post(
        "/api/ask", json={"question": "how does the kubernetes ingress shard traffic?"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "refused"
    assert payload["answer"] == REFUSAL
    assert payload["retrieval"]["chunks"] == []


def test_ask_without_model_returns_retrieval(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("atlas.web.detect_model", lambda timeout=None: None)

    payload = client.post("/api/ask", json={"question": "how does greet work?"}).json()

    assert payload["mode"] == "no-model"
    assert payload["message"] == NO_MODEL_MESSAGE
    assert payload["retrieval"]["chunks"]
    assert payload["retrieval"]["fused"]


def test_ask_with_model_returns_cited_answer(client: TestClient, monkeypatch) -> None:
    info = ModelInfo("fake", "fake-model", "http://127.0.0.1:9")
    monkeypatch.setattr("atlas.web.detect_model", lambda timeout=None: info)
    monkeypatch.setattr(
        "atlas.answer.complete",
        lambda _system, _prompt, _info: "`greet` calls `shout` (`app/service.py:3-5`).",
    )

    payload = client.post("/api/ask", json={"question": "how does greet work?"}).json()

    assert payload["mode"] == "answer"
    assert payload["trusted"] is True
    assert payload["citations"]["valid"] == ["app/service.py:3-5"]
    assert "app/service.py:3-5" in payload["answer"]
    assert payload["model"]["model"] == "fake-model"


def test_ask_catches_invented_citation(client: TestClient, monkeypatch) -> None:
    info = ModelInfo("fake", "fake-model", "http://127.0.0.1:9")
    monkeypatch.setattr("atlas.web.detect_model", lambda timeout=None: info)
    monkeypatch.setattr(
        "atlas.answer.complete",
        lambda _system, _prompt, _info: "Ghost (`app/service.py:900-999`).",
    )

    payload = client.post("/api/ask", json={"question": "how does greet work?"}).json()

    assert payload["mode"] == "answer"
    assert payload["citations"]["valid"] == []
    assert payload["citations"]["invalid"]
    assert "[invalid citation removed]" in payload["answer"]
    assert "900" not in payload["answer"]


def test_web_ask_matches_cli_ask(client: TestClient, repo: Path, monkeypatch) -> None:
    info = ModelInfo("fake", "fake-model", "http://127.0.0.1:9")
    text = "`greet` calls `shout` (`app/service.py:3-5`)."
    monkeypatch.setattr("atlas.web.detect_model", lambda timeout=None: info)
    monkeypatch.setattr("atlas.cli.detect_model", lambda: info)
    monkeypatch.setattr("atlas.answer.complete", lambda _system, _prompt, _info: text)

    web_payload = client.post("/api/ask", json={"question": "how does greet work?"}).json()

    cli_result = CliRunner().invoke(
        cli_app, ["ask", "how does greet work?", "--path", str(repo), "--json"]
    )

    assert cli_result.exit_code == 0, cli_result.output
    cli_payload = json.loads(cli_result.stdout)
    assert web_payload["answer"] == cli_payload["answer"]
    assert web_payload["citations"] == cli_payload["citations"]
    assert web_payload["retrieval"]["chunks"] == cli_payload["retrieval"]["chunks"]


def test_ask_validates_question_length(client: TestClient) -> None:
    assert client.post("/api/ask", json={"question": ""}).status_code == 422


# -- CLI ---------------------------------------------------------------


def test_cli_web_help_documents_flags() -> None:
    result = CliRunner().invoke(cli_app, ["web", "--help"])

    assert result.exit_code == 0, result.output
    assert "--port" in result.output
    assert "--no-browser" in result.output
    assert "127.0.0.1" in result.output


def test_cli_web_requires_an_index(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli_app, ["web", "--path", str(tmp_path), "--no-browser"])

    assert result.exit_code == 2
    assert "No graph found" in result.output
