from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from atlas.graph import build_graph, open_graph
from atlas.map import build_map, to_json, to_mermaid, to_text
from atlas.parse import parse_file, tree_sitter_available
from atlas.walker import scan


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def python_repo(tmp_path: Path) -> Path:
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
        "def greet(name):\n"
        "    return helper(name)\n\n"
        "def helper(name):\n"
        "    return 'hi ' + name\n",
    )
    return tmp_path


def _parse(root: Path, rel: str):
    info = next(f for f in scan(root).files if f.path == rel)
    parsed = parse_file(root, info)
    assert parsed is not None
    return parsed


# -- Python AST ---------------------------------------------------------


def test_python_symbols_and_methods(python_repo: Path) -> None:
    write(
        python_repo,
        "app/models.py",
        "class Base:\n"
        "    def save(self):\n"
        "        return 1\n\n"
        "class User(Base):\n"
        "    def save(self):\n"
        "        return super().save()\n",
    )
    parsed = _parse(python_repo, "app/models.py")
    names = {s.qualified_name: s for s in parsed.symbols}

    assert set(names) == {"Base", "Base.save", "User", "User.save"}
    assert names["Base.save"].kind == "method"
    assert names["User.save"].parent == "User"
    assert ("Base", "User", 5) in parsed.inherits


def test_python_calls_and_imports(python_repo: Path) -> None:
    parsed = _parse(python_repo, "app/main.py")

    assert [s.name for s in parsed.symbols] == ["main"]
    assert [(c.name, c.enclosing) for c in parsed.calls] == [
        ("greet", "main"),
        ("main", None),
    ]
    assert parsed.imports[0].module == "app.service"
    assert parsed.imports[0].name == "greet"


def test_python_signatures_include_decorators(python_repo: Path) -> None:
    write(
        python_repo,
        "app/web.py",
        "from functools import wraps\n\n"
        "@wraps\n"
        "def handler(request, response):\n"
        "    return None\n",
    )
    parsed = _parse(python_repo, "app/web.py")
    symbol = next(s for s in parsed.symbols if s.name == "handler")

    assert symbol.start_line == 3  # decorator line
    assert symbol.signature.startswith("def handler(request, response)")


def test_python_syntax_error_is_reported(tmp_path: Path) -> None:
    write(tmp_path, "broken.py", "def nope(:\n    pass\n")
    parsed = _parse(tmp_path, "broken.py")

    assert parsed.errors == 1
    assert parsed.symbols == []


# -- Graph --------------------------------------------------------------


def test_graph_builds_and_resolves(python_repo: Path) -> None:
    result = scan(python_repo)
    stats = build_graph(python_repo, result)

    assert stats.symbols == 3
    assert stats.edges >= 3
    assert stats.resolved_pct == pytest.approx(100.0)

    conn = open_graph(python_repo)
    try:
        rows = conn.execute(
            "SELECT e.dst_name, e.resolution, s.qualified_name"
            " FROM edges e JOIN symbols s ON s.id = e.dst_id"
            " ORDER BY e.dst_name"
        ).fetchall()
    finally:
        conn.close()

    resolved = {row["dst_name"]: (row["resolution"], row["qualified_name"]) for row in rows}
    assert resolved["greet"][1] == "greet"
    assert resolved["helper"] == ("local", "helper")
    assert resolved["main"][0] == "local"


def test_graph_handles_syntax_errors(tmp_path: Path) -> None:
    write(tmp_path, "ok.py", "def fine():\n    pass\n")
    write(tmp_path, "bad.py", "def broken(:\n")
    stats = build_graph(tmp_path, scan(tmp_path))

    assert stats.parse_errors == 1
    assert stats.symbols == 1


# -- Map ----------------------------------------------------------------


def test_map_falls_back_to_file_level(python_repo: Path) -> None:
    build_graph(python_repo, scan(python_repo))
    data = build_map(python_repo)  # package level has no cross-package edges

    assert data.level == "file"
    assert {node.key for node in data.nodes} == {"app/main.py", "app/service.py"}
    assert any(edge.src == "app/main.py" and edge.dst == "app/service.py" for edge in data.edges)


def test_map_formats(python_repo: Path) -> None:
    build_graph(python_repo, scan(python_repo))
    data = build_map(python_repo, level="file")

    mermaid = to_mermaid(data)
    assert mermaid.startswith("graph LR")
    assert "-->" in mermaid

    text = to_text(data)
    assert "app/main.py" in text and "app/service.py" in text

    payload = json.loads(to_json(data))
    assert payload["level"] == "file"
    assert payload["edges"]


def test_map_requires_graph(python_repo: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_map(python_repo)


# -- tree-sitter languages ---------------------------------------------


needs_treesitter = pytest.mark.skipif(
    not tree_sitter_available(), reason="tree-sitter grammars unavailable"
)


@needs_treesitter
def test_typescript_symbols_and_calls(tmp_path: Path) -> None:
    write(
        tmp_path,
        "src/app.ts",
        'import { helper } from "./util";\n\n'
        "export function greet(name: string) {\n"
        "  return helper(name);\n"
        "}\n\n"
        "class Service {\n"
        "  run() { return greet('x'); }\n"
        "}\n",
    )
    parsed = _parse(tmp_path, "src/app.ts")
    names = {s.qualified_name: s for s in parsed.symbols}

    assert "greet" in names
    assert names["Service.run"].kind == "method"
    assert parsed.imports[0].module == "./util"
    assert {c.name for c in parsed.calls} >= {"helper", "greet"}


@needs_treesitter
def test_go_symbols_and_calls(tmp_path: Path) -> None:
    write(
        tmp_path,
        "cmd/main.go",
        "package main\n\n"
        'import "fmt"\n\n'
        "func main() {\n"
        "    fmt.Println(greet())\n"
        "}\n\n"
        "func greet() string { return \"hi\" }\n",
    )
    parsed = _parse(tmp_path, "cmd/main.go")
    names = {s.name: s for s in parsed.symbols}

    assert set(names) == {"main", "greet"}
    assert parsed.imports[0].module == "fmt"
    assert {c.name for c in parsed.calls} >= {"Println", "greet"}
