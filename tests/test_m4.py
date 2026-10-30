"""M4: incremental indexing, diff, and incremental/full parity."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from atlas.cli import app as cli_app
from atlas.diff import compute_diff, render_text, to_dict
from atlas.entrypoints import find_entrypoints
from atlas.graph import graph_path, open_graph, update_graph
from atlas.manifest import build_manifest, read_manifest, write_manifest
from atlas.walker import scan
from atlas.web import create_app


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def index(root: Path, *, full: bool = False):
    """Exactly what ``atlas index`` does: graph first, then the manifest."""
    scanned = scan(root)
    stats = update_graph(root, scanned, read_manifest(root), force_full=full)
    write_manifest(root, build_manifest(scanned, find_entrypoints(root, scanned.files)))
    return stats


def cli(*args: str):
    return CliRunner().invoke(cli_app, list(args))


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    write(
        tmp_path,
        "app/main.py",
        "from app.service import greet\n\n\ndef main():\n    greet('world')\n",
    )
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
    return tmp_path


# -- read-only snapshots ------------------------------------------------


def query(root: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = open_graph(root)
    try:
        return [tuple(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def symbol_rows(root: Path) -> list[tuple]:
    return query(
        root,
        "SELECT f.path, s.qualified_name, s.name, s.kind, s.start_line,"
        " s.end_line, s.signature, s.docstring,"
        " COALESCE(p.qualified_name, ''), s.exported"
        " FROM symbols s JOIN files f ON f.id = s.file_id"
        " LEFT JOIN symbols p ON p.id = s.parent_id"
        " ORDER BY f.path, s.start_line, s.qualified_name",
    )


def chunk_rows(root: Path) -> list[tuple]:
    return query(
        root,
        "SELECT c.path, c.name, c.kind, c.start_line, c.end_line, c.text,"
        " c.docstring, c.tokens FROM chunks c ORDER BY c.path, c.start_line, c.name",
    )


def edge_rows(root: Path) -> list[tuple]:
    return query(
        root,
        "SELECT f.path, e.kind, e.dst_name, COALESCE(e.receiver, ''), e.line,"
        " COALESCE(src.qualified_name, ''), COALESCE(dst.qualified_name, ''),"
        " COALESCE(e.resolution, '')"
        " FROM edges e JOIN files f ON f.id = e.file_id"
        " LEFT JOIN symbols src ON src.id = e.src_id"
        " LEFT JOIN symbols dst ON dst.id = e.dst_id"
        " ORDER BY f.path, e.line, e.kind, e.dst_name, 6, 7, 8",
    )


def import_rows(root: Path) -> list[tuple]:
    return query(
        root,
        "SELECT f.path, i.module, COALESCE(i.name, ''), COALESCE(i.alias, ''),"
        " i.level, i.line, COALESCE(d.path, '')"
        " FROM imports i JOIN files f ON f.id = i.file_id"
        " LEFT JOIN files d ON d.id = i.dst_file_id"
        " ORDER BY f.path, i.line, i.module, 3, 4",
    )


def fts_rows(root: Path) -> list[tuple]:
    return query(
        root,
        "SELECT f.path, c.name, c.text, c.docstring FROM chunks_fts"
        " JOIN chunks c ON c.id = chunks_fts.rowid"
        " JOIN files f ON f.id = c.file_id"
        " ORDER BY f.path, c.name",
    )


def raw_symbols(root: Path) -> list[tuple]:
    return query(
        root,
        "SELECT s.id, f.path, s.file_id, s.name, s.qualified_name, s.kind,"
        " s.start_line, s.end_line, s.signature, s.docstring, s.parent_id,"
        " s.exported FROM symbols s JOIN files f ON f.id = s.file_id"
        " ORDER BY s.id",
    )


def raw_chunks(root: Path) -> list[tuple]:
    return query(
        root,
        "SELECT c.id, f.path, c.file_id, c.symbol_id, c.name, c.kind,"
        " c.start_line, c.end_line, c.text, c.docstring, c.tokens"
        " FROM chunks c JOIN files f ON f.id = c.file_id ORDER BY c.id",
    )


def raw_edges(root: Path) -> list[tuple]:
    return query(
        root,
        "SELECT e.id, f.path, e.src_id, e.dst_id, e.dst_name, e.receiver,"
        " e.kind, e.file_id, e.line, e.resolution"
        " FROM edges e JOIN files f ON f.id = e.file_id ORDER BY e.id",
    )


def raw_imports(root: Path) -> list[tuple]:
    return query(
        root,
        "SELECT i.id, f.path, i.file_id, i.module, i.name, i.alias, i.level,"
        " i.line, i.dst_file_id FROM imports i JOIN files f ON f.id = i.file_id"
        " ORDER BY i.id",
    )


def raw_files(root: Path) -> list[tuple]:
    return query(root, "SELECT * FROM files ORDER BY id")


def raw_fts(root: Path) -> list[tuple]:
    return query(root, "SELECT rowid, name, path, text, docstring FROM chunks_fts ORDER BY rowid")


def for_path(rows: list[tuple], path: str) -> list[tuple]:
    return [row for row in rows if row[1] == path]


def raw_for_paths(root: Path, paths: set[str]) -> dict[str, list[tuple]]:
    return {
        "files": [row for row in raw_files(root) if row[1] in paths],
        "symbols": [row for row in raw_symbols(root) if row[1] in paths],
        "chunks": [row for row in raw_chunks(root) if row[1] in paths],
        "edges": [row for row in raw_edges(root) if row[1] in paths],
        "imports": [row for row in raw_imports(root) if row[1] in paths],
    }


def snapshot(root: Path) -> dict[str, list[tuple]]:
    return {
        "symbols": symbol_rows(root),
        "chunks": chunk_rows(root),
        "edges": edge_rows(root),
        "imports": import_rows(root),
        "fts": fts_rows(root),
    }


# -- incremental indexing ----------------------------------------------


def test_second_index_parses_nothing_and_keeps_every_row(repo: Path) -> None:
    first = index(repo)
    assert first.full is True
    assert first.warning is not None
    assert first.parsed == first.files == len(scan(repo).files)

    before = snapshot(repo)
    raw_before = (
        raw_files(repo),
        raw_symbols(repo),
        raw_chunks(repo),
        raw_edges(repo),
        raw_imports(repo),
        raw_fts(repo),
    )

    second = index(repo)

    assert second.parsed == 0
    assert second.skipped == second.files == len(scan(repo).files)
    assert second.deleted == 0
    assert second.full is False
    assert second.warning is None
    assert snapshot(repo) == before
    assert (
        raw_files(repo),
        raw_symbols(repo),
        raw_chunks(repo),
        raw_edges(repo),
        raw_imports(repo),
        raw_fts(repo),
    ) == raw_before


def test_changed_file_reparses_alone_and_keeps_unchanged_rows(repo: Path) -> None:
    index(repo)
    untouched = {"app/main.py", "util/text.py", "README.md"}
    before = raw_for_paths(repo, untouched)
    old_symbols = symbol_rows(repo)

    write(
        repo,
        "app/service.py",
        "from util.text import shout\n\n\n"
        "def greet(name):\n"
        '    """Say hello to a person."""\n'
        '    return shout("hi " + name)\n\n\n'
        "def helper(name):\n"
        "    return name\n\n\n"
        "def farewell(name):\n"
        '    return shout("bye " + name)\n',
    )

    stats = index(repo)

    assert stats.parsed == 1
    assert stats.changed == 1
    assert stats.added == 0
    assert stats.deleted == 0
    assert stats.skipped == stats.files - 1
    assert stats.full is False
    after = raw_for_paths(repo, untouched)
    for table in ("files", "symbols", "chunks", "imports"):
        assert after[table] == before[table]
    # Edges in unchanged files keep their identity; only the derived target id
    # and resolution may move when the changed file is re-resolved.
    assert [row[:3] + row[4:9] for row in after["edges"]] == [
        row[:3] + row[4:9] for row in before["edges"]
    ]

    after_symbols = symbol_rows(repo)
    assert ("app/service.py", "farewell", "farewell", "function") in [
        row[:4] for row in after_symbols
    ]
    assert len(after_symbols) == len(old_symbols) + 1
    # The unchanged files kept the exact same symbols, including ids.
    untouched_before = [row for row in raw_symbols(repo) if row[1] in untouched]
    assert {row[1] for row in untouched_before} == {"app/main.py", "util/text.py"}


def test_added_file_adds_and_deleted_file_removes_rows(repo: Path) -> None:
    index(repo)

    write(
        repo,
        "app/extra.py",
        "from app.service import helper\n\n\ndef extra():\n    return helper('x')\n",
    )
    stats = index(repo)

    assert stats.added == 1
    assert stats.parsed == 1
    assert stats.skipped == stats.files - 1
    assert any(row[0] == "app/extra.py" and row[2] == "extra" for row in symbol_rows(repo))
    assert any(row[0] == "app/extra.py" for row in chunk_rows(repo))
    extra_edges = [row for row in edge_rows(repo) if row[0] == "app/extra.py"]
    assert any(row[1] == "call" and row[2] == "helper" and row[7] == "import" for row in extra_edges)
    assert any(row[0] == "app/extra.py" and row[6] == "app/service.py" for row in import_rows(repo))

    write(
        repo,
        "app/gone.py",
        "from app.service import helper\n\n\ndef temporary():\n    return helper('gone')\n",
    )
    index(repo)
    assert any(row[0] == "app/gone.py" for row in symbol_rows(repo))

    (repo / "app/gone.py").unlink()
    stats = index(repo)

    assert stats.deleted == 1
    assert stats.parsed == 0
    assert stats.skipped == stats.files
    assert not any(row[0] == "app/gone.py" for row in raw_files(repo))
    assert not any(row[0] == "app/gone.py" for row in symbol_rows(repo))
    assert not any(row[0] == "app/gone.py" for row in chunk_rows(repo))
    assert not any(row[0] == "app/gone.py" for row in edge_rows(repo))
    assert not any(row[0] == "app/gone.py" for row in import_rows(repo))
    assert not any(row[0] == "app/gone.py" for row in fts_rows(repo))
    # The added file and its edges are untouched by the deletion.
    assert any(row[0] == "app/extra.py" and row[2] == "extra" for row in symbol_rows(repo))
    assert any(row[0] == "app/extra.py" and row[2] == "helper" for row in edge_rows(repo))


def test_manifest_ahead_of_graph_forces_rebuild(repo: Path) -> None:
    index(repo)
    write(repo, "app/service.py", "def greet(name):\n    return name\n")
    result = cli("index", str(repo), "--no-graph")
    assert result.exit_code == 0, result.output

    stats = index(repo)

    assert stats.full is True
    assert stats.warning is not None
    assert "Manifest and graph disagree" in stats.warning
    assert stats.parsed == stats.files


# -- diff ---------------------------------------------------------------


def test_diff_reports_files_symbols_and_edges(repo: Path) -> None:
    index(repo)
    write(
        repo,
        "app/service.py",
        "from util.text import shout\n\n\n"
        "def greet(name, punctuation='!'):\n"
        '    """Say hello to a person."""\n'
        '    return shout("hi " + name) + punctuation\n\n\n'
        "def helper(name):\n"
        "    return name\n",
    )
    write(repo, "app/extra.py", "def extra():\n    return 1\n")
    (repo / "util/text.py").unlink()

    result = compute_diff(repo)

    assert not result.clean
    assert result.files_added == ["app/extra.py"]
    assert result.files_deleted == ["util/text.py"]
    assert [change.path for change in result.files_changed] == ["app/service.py"]
    assert result.files_changed[0].old_sha != result.files_changed[0].new_sha

    assert [(s.path, s.name, s.kind) for s in result.symbols_added] == [
        ("app/extra.py", "extra", "function")
    ]
    assert [(s.path, s.name, s.kind) for s in result.symbols_removed] == [
        ("util/text.py", "shout", "function")
    ]
    assert [(s.path, s.name, s.kind) for s in result.symbols_changed] == [
        ("app/service.py", "greet", "function")
    ]
    assert result.symbols_changed[0].line == 4
    # Removed source lines shift the greeting call inside greet.
    assert {edge.path for edge in result.edges_removed} == {"util/text.py"}
    assert {edge.path for edge in result.edges_added} <= {"app/extra.py", "app/service.py"}


def test_diff_text_and_json_match_and_do_not_write(repo: Path) -> None:
    index(repo)
    write(repo, "app/service.py", "def greet(name):\n    return name\n")
    write(repo, "app/extra.py", "def extra():\n    return 1\n")
    (repo / "util/text.py").unlink()

    manifest_path = repo / ".atlas" / "manifest.json"
    manifest_before = manifest_path.read_bytes()
    graph_before = snapshot(repo)

    text_result = cli("diff", str(repo))
    json_result = cli("diff", str(repo), "--json")

    assert text_result.exit_code == 0, text_result.output
    assert json_result.exit_code == 0, json_result.output
    text = text_result.stdout
    payload = json.loads(json_result.stdout)

    assert payload["clean"] is False
    assert payload["files"]["added"] == ["app/extra.py"]
    assert payload["files"]["deleted"] == ["util/text.py"]
    changed = payload["files"]["changed"]
    assert [entry["path"] for entry in changed] == ["app/service.py"]
    assert changed[0]["old_sha256"] != changed[0]["new_sha256"]
    assert [(entry["path"], entry["name"], entry["kind"]) for entry in payload["symbols"]["added"]] == [
        ("app/extra.py", "extra", "function")
    ]
    removed = [(entry["path"], entry["name"]) for entry in payload["symbols"]["removed"]]
    assert removed == [("app/service.py", "helper"), ("util/text.py", "shout")]
    assert [(entry["path"], entry["name"]) for entry in payload["symbols"]["changed"]] == [
        ("app/service.py", "greet")
    ]
    assert payload["symbols"]["changed"][0]["old_line"] == 4
    assert all(edge["count"] > 0 for edge in payload["edges"]["added"])
    assert all(edge["count"] > 0 for edge in payload["edges"]["removed"])

    # Text and JSON describe the same groups.
    for path in payload["files"]["added"] + payload["files"]["deleted"]:
        assert path in text
    for entry in changed:
        assert entry["path"] in text
        assert entry["old_sha256"][:12] in text
        assert entry["new_sha256"][:12] in text
    for group in ("added", "removed", "changed"):
        entries = payload["symbols"][group]
        for entry in entries:
            assert f"{entry['path']}:{entry['line']}" in text
            assert f"{entry['name']} ({entry['kind']})" in text
    assert f"added ({len(payload['files']['added'])})" in text
    assert f"deleted ({len(payload['files']['deleted'])})" in text
    assert f"changed ({len(payload['files']['changed'])})" in text
    assert f"added ({len(payload['symbols']['added'])})" in text
    assert f"removed ({len(payload['symbols']['removed'])})" in text
    assert f"changed ({len(payload['symbols']['changed'])})" in text

    assert manifest_path.read_bytes() == manifest_before
    assert snapshot(repo) == graph_before


def test_diff_is_empty_on_a_clean_repo(repo: Path) -> None:
    index(repo)

    text = cli("diff", str(repo))
    payload = json.loads(cli("diff", str(repo), "--json").stdout)

    assert text.exit_code == 0, text.output
    assert "No changes" in text.stdout
    assert payload["clean"] is True
    assert payload["files"] == {"added": [], "deleted": [], "changed": []}
    assert payload["symbols"] == {"added": [], "removed": [], "changed": []}
    assert payload["edges"] == {"added": [], "removed": []}


def test_diff_without_an_index_is_a_clean_error(tmp_path: Path) -> None:
    result = cli("diff", str(tmp_path))

    assert result.exit_code == 2
    assert "No index found" in result.output


# -- acceptance: incremental, diff, full parity ------------------------


def test_acceptance_incremental_matches_full_rebuild(repo: Path) -> None:
    first = index(repo)
    assert first.full is True

    # Change exactly one file, add one, delete one.
    write(
        repo,
        "app/service.py",
        "from util.text import shout\n\n\n"
        "def greet(name):\n"
        '    """Say hello to a person."""\n'
        '    return shout("hi " + name)\n\n\n'
        "def helper(name):\n"
        "    return name\n\n\n"
        "def farewell(name):\n"
        '    return shout("bye " + name)\n',
    )
    write(repo, "app/extra.py", "from app.service import helper\n\n\ndef extra():\n    return helper('x')\n")
    (repo / "util/text.py").unlink()

    incremental = index(repo)

    assert incremental.full is False
    assert incremental.parsed == 2  # changed service.py + new extra.py
    assert incremental.changed == 1
    assert incremental.added == 1
    assert incremental.deleted == 1
    assert incremental.skipped == incremental.files - 2

    diff = compute_diff(repo)
    assert diff.clean is True
    assert render_text(diff).count("No changes") == 1

    # Diff before the re-index named exactly the changed file and symbols.
    write(repo, "app/service.py", "def greet(name, punctuation='!'):\n    return name + punctuation\n")
    pending = compute_diff(repo)
    assert [change.path for change in pending.files_changed] == ["app/service.py"]
    assert [(s.path, s.name) for s in pending.symbols_changed] == [
        ("app/service.py", "greet")
    ]
    assert ("app/service.py", "farewell") in [
        (s.path, s.name) for s in pending.symbols_removed
    ]

    # Re-index the change, then compare with a forced full rebuild.
    incremental = index(repo)
    assert incremental.full is False
    assert incremental.parsed == 1
    incremental_rows = snapshot(repo)

    full = index(repo, full=True)
    assert full.full is True
    assert full.parsed == full.files

    assert snapshot(repo) == incremental_rows
    assert read_manifest(repo)["summary"]["files"] == full.files


# -- fallbacks ----------------------------------------------------------


def _corrupt_manifest(repo: Path) -> None:
    (repo / ".atlas" / "manifest.json").write_text("{not json", encoding="utf-8")


def _remove_manifest(repo: Path) -> None:
    (repo / ".atlas" / "manifest.json").unlink()


def _remove_graph(repo: Path) -> None:
    target = graph_path(repo)
    target.unlink()
    for suffix in ("-wal", "-shm"):
        leftover = target.with_name(target.name + suffix)
        if leftover.exists():
            leftover.unlink()


def _corrupt_graph(repo: Path) -> None:
    target = graph_path(repo)
    _remove_graph(repo)
    target.write_bytes(b"this is not a sqlite database")


@pytest.mark.parametrize(
    ("damage", "warning"),
    [
        (_remove_manifest, "Manifest missing or unreadable"),
        (_corrupt_manifest, "Manifest missing or unreadable"),
        (_remove_graph, "Graph missing"),
        (_corrupt_graph, "Graph is unreadable"),
    ],
)
def test_bad_manifest_or_graph_falls_back_to_full(repo: Path, damage, warning: str) -> None:
    index(repo)
    baseline = snapshot(repo)
    file_count = len(scan(repo).files)

    damage(repo)
    stats = index(repo)

    assert stats.full is True
    assert stats.warning is not None
    assert warning in stats.warning
    assert stats.parsed == stats.files == file_count
    assert stats.skipped == 0
    assert snapshot(repo) == baseline

    conn = open_graph(repo)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_web_reads_the_incremental_graph(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr("atlas.web.detect_model", lambda timeout=None: None)
    index(repo)
    client = TestClient(create_app(repo))

    write(repo, "app/extra.py", "def extra():\n    return 1\n")
    (repo / "util/text.py").unlink()
    stats = index(repo)
    assert stats.full is False
    assert stats.parsed == 1
    assert stats.deleted == 1

    meta = client.get("/api/meta").json()
    indexed = [row[0] for row in query(repo, "SELECT path FROM files ORDER BY path")]
    assert [entry["path"] for entry in meta["tree"]] == indexed
    assert meta["files"] == stats.files == len(indexed)
    assert client.get("/api/file", params={"path": "app/extra.py"}).status_code == 200
    assert client.get("/api/file", params={"path": "util/text.py"}).status_code == 404

    map_payload = client.get("/api/map", params={"level": "file"}).json()
    assert any(node["key"] == "app/extra.py" for node in map_payload["nodes"])

    ask = client.post("/api/ask", json={"question": "what does extra do?"}).json()
    assert ask["mode"] == "no-model"
    assert any(chunk["path"] == "app/extra.py" for chunk in ask["retrieval"]["chunks"])


def test_incremental_database_error_falls_back_without_half_index(repo: Path) -> None:
    index(repo)
    baseline = snapshot(repo)

    # Break a table the incremental path reads, without disturbing the meta and
    # files tables the preflight check uses. The failure must fall back to a
    # complete rebuild rather than leave a half index.
    conn = sqlite3.connect(graph_path(repo))
    try:
        conn.execute("DROP TABLE chunks")
        conn.commit()
    finally:
        conn.close()

    stats = index(repo)

    assert stats.full is True
    assert stats.warning is not None
    assert "rebuilding the full index" in stats.warning
    assert snapshot(repo) == baseline
    conn = open_graph(repo)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


# -- CLI reporting ------------------------------------------------------


def test_cli_index_json_reports_parsed_skipped_deleted(repo: Path) -> None:
    first = json.loads(cli("index", str(repo), "--json").stdout)
    assert first["graph"]["full"] is True
    assert first["graph"]["parsed"] == first["graph"]["files"]
    assert first["graph"]["skipped"] == 0
    assert first["graph"]["deleted"] == 0
    assert first["graph"]["warning"]

    second = json.loads(cli("index", str(repo), "--json").stdout)
    assert second["graph"]["full"] is False
    assert second["graph"]["parsed"] == 0
    assert second["graph"]["skipped"] == second["graph"]["files"]
    assert second["graph"]["deleted"] == 0
    assert second["graph"]["warning"] is None

    write(repo, "app/extra.py", "def extra():\n    return 1\n")
    (repo / "README.md").unlink()
    third = json.loads(cli("index", str(repo), "--json").stdout)
    assert third["graph"]["parsed"] == 1
    assert third["graph"]["added"] == 1
    assert third["graph"]["deleted"] == 1
    assert third["graph"]["skipped"] == third["graph"]["files"] - 1

    forced = json.loads(cli("index", str(repo), "--json", "--full").stdout)
    assert forced["graph"]["full"] is True
    assert forced["graph"]["parsed"] == forced["graph"]["files"]
    assert forced["graph"]["skipped"] == 0


def test_cli_index_text_still_shows_the_new_counters(repo: Path) -> None:
    cli("index", str(repo))
    output = cli("index", str(repo)).stdout

    assert "parsed" in output
    assert "skipped" in output
    assert "deleted" in output
    assert "0 parsed" in output
