from __future__ import annotations

import json
from pathlib import Path

from atlas.entrypoints import find_entrypoints, is_test_path
from atlas.manifest import build_manifest, write_manifest
from atlas.walker import scan


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_gitignore_and_negation(tmp_path: Path) -> None:
    write(tmp_path, ".gitignore", "*.log\n!keep.log\n")
    write(tmp_path, "debug.log", "noise\n")
    write(tmp_path, "keep.log", "signal\n")
    write(tmp_path, "app.py", "print('hi')\n")

    result = scan(tmp_path)
    paths = {f.path for f in result.files}

    assert "app.py" in paths
    assert "keep.log" in paths
    assert "debug.log" not in paths


def test_nested_gitignore(tmp_path: Path) -> None:
    write(tmp_path, ".gitignore", "*.tmp\n")
    write(tmp_path, "pkg/.gitignore", "secret.py\n")
    write(tmp_path, "pkg/secret.py", "x = 1\n")
    write(tmp_path, "pkg/public.py", "y = 2\n")

    paths = {f.path for f in scan(tmp_path).files}
    assert "pkg/public.py" in paths
    assert "pkg/secret.py" not in paths


def test_default_excludes(tmp_path: Path) -> None:
    write(tmp_path, "node_modules/pkg/index.js", "module.exports = 1\n")
    write(tmp_path, "build/out.py", "x = 1\n")
    write(tmp_path, "src/main.py", "x = 1\n")

    paths = {f.path for f in scan(tmp_path).files}
    assert paths == {"src/main.py"}


def test_binary_and_large_skipped(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    write(tmp_path, "big.py", "x = 1\n" * 100)
    write(tmp_path, "small.py", "x = 1\n")

    result = scan(tmp_path, max_file_bytes=50)
    paths = {f.path for f in result.files}

    assert "small.py" in paths
    assert "big.py" not in paths
    assert "blob.bin" not in paths
    assert result.skipped["binary"] == 1
    assert result.skipped["large"] == 1


def test_line_counting_and_hashes(tmp_path: Path) -> None:
    write(tmp_path, "a.py", "one\ntwo\nthree")
    write(tmp_path, "b.py", "one\ntwo\nthree\n")

    result = scan(tmp_path)
    by_path = {f.path: f for f in result.files}

    assert by_path["a.py"].lines == 3
    assert by_path["b.py"].lines == 3
    assert by_path["a.py"].sha256 != by_path["b.py"].sha256
    assert result.code_lines == 6


def test_shebang_detection(tmp_path: Path) -> None:
    path = tmp_path / "run"
    path.write_text("#!/usr/bin/env python3\nprint('hi')\n", encoding="utf-8")

    result = scan(tmp_path)
    assert result.files[0].language == "Python"
    assert result.files[0].shebang is not None


def test_python_main_guard(tmp_path: Path) -> None:
    write(tmp_path, "app.py", "def main():\n    pass\n\nif __name__ == '__main__':\n    main()\n")
    result = scan(tmp_path)
    entries = find_entrypoints(tmp_path, result.files)

    assert any(e.kind == "script" and e.path == "app.py" and e.line == 4 for e in entries)


def test_server_detection(tmp_path: Path) -> None:
    write(tmp_path, "server.py", "from fastapi import FastAPI\n\napp = FastAPI()\n")
    result = scan(tmp_path)
    entries = find_entrypoints(tmp_path, result.files)

    assert any(e.kind == "server" and e.path == "server.py" for e in entries)


def test_go_main(tmp_path: Path) -> None:
    write(tmp_path, "cmd/tool/main.go", "package main\n\nfunc main() {\n}\n")
    result = scan(tmp_path)
    entries = find_entrypoints(tmp_path, result.files)

    assert any(e.kind == "binary" and e.path == "cmd/tool/main.go" for e in entries)


def test_test_paths() -> None:
    assert is_test_path("tests/test_api.py")
    assert is_test_path("pkg/__tests__/button.tsx")
    assert is_test_path("pkg/api.test.ts")
    assert is_test_path("src/FooTest.java")
    assert not is_test_path("src/testing_utils.py")
    assert not is_test_path("src/api.py")


def test_manifest_roundtrip(tmp_path: Path) -> None:
    write(tmp_path, "main.py", "if __name__ == '__main__':\n    pass\n")
    result = scan(tmp_path)
    entries = find_entrypoints(tmp_path, result.files)
    manifest = build_manifest(result, entries)

    json.dumps(manifest)
    assert manifest["schema"] == 1
    assert manifest["summary"]["files"] == 1
    assert manifest["files"][0]["path"] == "main.py"

    target = write_manifest(tmp_path, manifest)
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8"))["summary"]["files"] == 1
