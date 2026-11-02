"""Contracts behind the three map/citation/line-count fixes.

The map is drawn in the browser, so these tests pin the parts a Python test can
reach: the /api/map payload the renderer consumes, the absence of any
third-party map script, the citation label format the linkifier matches, and
indexed-versus-live line counts agreeing after a re-index. The DOM behaviour
itself was checked in a real browser.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.answer import validate_citations
from atlas.entrypoints import find_entrypoints
from atlas.graph import build_graph
from atlas.manifest import build_manifest, write_manifest
from atlas.retrieval import RetrievalChunk
from atlas.walker import scan
from atlas.web import STATIC_DIR, create_app


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def reindex(root: Path) -> None:
    scanned = scan(root)
    build_graph(root, scanned)
    manifest = build_manifest(scanned, find_entrypoints(root, scanned.files))
    write_manifest(root, manifest)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    write(tmp_path, "app/main.py", "from app.service import greet\n\n\ndef main():\n    greet('world')\n")
    write(
        tmp_path,
        "app/service.py",
        "from util.text import shout\n\n\ndef greet(name):\n"
        '    """Say hello to a person."""\n'
        '    return shout("hi " + name)\n',
    )
    write(tmp_path, "util/text.py", "def shout(text):\n    return text.upper()\n")
    write(tmp_path, "README.md", "# demo\n\nA tiny repo.\n")
    reindex(tmp_path)
    return tmp_path


@pytest.fixture()
def client(repo: Path) -> TestClient:
    return TestClient(create_app(repo))


# -- map rendering -------------------------------------------------------


def test_map_payload_can_be_drawn_in_both_views(client: TestClient) -> None:
    """Every node needs the fields the renderer prints, and every edge must
    point at nodes that exist, or the layout drops connections silently."""
    for level in ("file", "package"):
        payload = client.get(f"/api/map?level={level}").json()
        assert payload["nodes"], f"no nodes for level={level}"
        for node in payload["nodes"]:
            assert node["key"]
            assert isinstance(node["files"], int)
            assert isinstance(node["symbols"], int)
        keys = {node["key"] for node in payload["nodes"]}
        for edge in payload["edges"]:
            assert edge["src"] in keys
            assert edge["dst"] in keys
            assert edge["count"] >= 1


def test_map_needs_no_third_party_script_and_keeps_a_fallback() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "mermaid" not in html.lower(), "index.html must not load a third-party map script"
    assert "vendor/" not in html, "no vendored runtime should be required to draw the map"
    assert not (STATIC_DIR / "vendor").exists()

    # Primary renderer, list fallback, and a package-to-file resolver.
    for name in ("renderMapSvg", "renderMapList", "resolveMapTarget"):
        assert name in script
    assert "ensureMermaid" not in script

    # The meta count must come from the links actually rendered.
    assert "linkifyAnswerCitations" in script
    assert 'querySelectorAll("a.cite").length' in script


# -- citations in answer text -------------------------------------------


def test_validated_citation_survives_inside_a_fence_as_a_linkable_label() -> None:
    """A citation quoted in a fenced block is still validated, and its label
    must match the exact shape the front-end turns into a link."""
    chunk = RetrievalChunk(
        chunk_id=1,
        symbol_id=None,
        path="app/service.py",
        name="greet",
        kind="function",
        start=1,
        end=8,
        text="",
        docstring="",
        tokens=10,
    )
    text = (
        "Here is the code:\n\n"
        "```python\n"
        "def greet(name):\n"
        "    return name\n"
        "```\n\n"
        "It lives in app/service.py:1-8.\n"
    )

    clean, report = validate_citations(text, [chunk], {"app/service.py": 20})

    assert report.valid == ["app/service.py:1-8"]
    assert report.invalid == []
    assert "app/service.py:1-8" in clean
    assert re.fullmatch(r".+:\d+(?:-\d+)?", report.valid[0])


def test_unvalidated_citation_is_removed_instead_of_presented() -> None:
    chunk = RetrievalChunk(
        chunk_id=1,
        symbol_id=None,
        path="app/service.py",
        name="greet",
        kind="function",
        start=1,
        end=8,
        text="",
        docstring="",
        tokens=10,
    )

    clean, report = validate_citations(
        "See app/service.py:99-120 for details.", [chunk], {"app/service.py": 20}
    )

    assert report.valid == []
    assert report.invalid
    assert "app/service.py:99-120" not in clean


# -- indexed versus live line counts ------------------------------------


def test_tree_and_viewer_line_counts_agree_after_reindex(
    client: TestClient, repo: Path
) -> None:
    """The tree reports the index snapshot and the viewer reads disk, so the
    only way they agree is a fresh index. This pins that they do."""
    meta = client.get("/api/meta").json()
    indexed = next(item for item in meta["tree"] if item["path"] == "app/service.py")
    live = client.get("/api/file", params={"path": "app/service.py"}).json()
    assert indexed["lines"] == live["total_lines"]

    # Grow the file, re-index, and confirm both numbers move together.
    target = write(repo, "app/service.py", (repo / "app/service.py").read_text(encoding="utf-8") + "\n\n\ndef extra():\n    return 1\n")
    reindex(repo)

    meta = client.get("/api/meta").json()
    indexed = next(item for item in meta["tree"] if item["path"] == "app/service.py")
    live = client.get("/api/file", params={"path": "app/service.py"}).json()
    assert indexed["lines"] == live["total_lines"]
    assert live["total_lines"] == len(target.read_text(encoding="utf-8").splitlines())
