"""Entry-point heuristics.

M0 only needs to answer "where does execution start?" well enough to make the
repo legible. Every hit carries a score so rendering can keep the signal and
drop the noise.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .walker import FileInfo

MAX_SCAN_BYTES = 256 * 1024
PYTHON_SCAN_LIMIT = 2000

MAIN_GUARD = re.compile(r"""^[ \t]*if\s+__name__\s*==\s*['"]__main__['"]""", re.MULTILINE)
PY_SERVER_IMPORT = re.compile(
    r"^[ \t]*(?:from|import)\s+(fastapi|flask|quart|litestar|starlette)\b",
    re.MULTILINE | re.IGNORECASE,
)
PY_SERVER_CALL = re.compile(r"\b(FastAPI|Flask|Quart|Litestar|Starlette)\s*\(")
UVICORN_RUN = re.compile(r"\buvicorn\.run\s*\(")
GO_MAIN = re.compile(r"^func\s+main\s*\(\s*\)", re.MULTILINE)
GO_PACKAGE_MAIN = re.compile(r"^package\s+main\b", re.MULTILINE)
RUST_MAIN = re.compile(r"^[ \t]*fn\s+main\s*\(", re.MULTILINE)
C_MAIN = re.compile(r"^[ \t]*int\s+main\s*\(", re.MULTILINE)
JAVA_MAIN = re.compile(r"public\s+static\s+void\s+main\s*\(")
CS_MAIN = re.compile(r"static\s+(?:async\s+)?(?:void|int|Task)\s+Main\s*\(", re.IGNORECASE)
KT_MAIN = re.compile(r"^[ \t]*fun\s+main\s*\(", re.MULTILINE)

TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs"}
TEST_FILE = re.compile(
    r"(^test_.*\.py$|.*_test\.(py|go|rb|exs)$|.*\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$"
    r"|.*(Test|Tests|IT)\.(java|kt|cs)$)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class EntryPoint:
    kind: str
    path: str
    line: int
    detail: str
    score: float


def _read_text(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > MAX_SCAN_BYTES:
        data = data[:MAX_SCAN_BYTES]
    return data.decode("utf-8", errors="replace")


def is_test_path(rel_path: str) -> bool:
    path = Path(rel_path)
    if any(part.lower() in TEST_DIRS for part in path.parts[:-1]):
        return True
    return bool(TEST_FILE.match(path.name))


def _first_line(regex: re.Pattern[str], text: str) -> int | None:
    match = regex.search(text)
    if not match:
        return None
    return text.count("\n", 0, match.start()) + 1


def find_entrypoints(root: Path, files: list[FileInfo]) -> list[EntryPoint]:
    root = Path(root)
    found: dict[str, EntryPoint] = {}

    def add(entry: EntryPoint) -> None:
        current = found.get(entry.path)
        if current is None or entry.score > current.score:
            found[entry.path] = entry

    python_files = [f for f in files if f.language == "Python"][:PYTHON_SCAN_LIMIT]

    for info in python_files:
        if is_test_path(info.path):
            continue
        text = _read_text(root / info.path)
        name = Path(info.path).name

        line = _first_line(MAIN_GUARD, text)
        if line:
            add(EntryPoint("script", info.path, line, 'if __name__ == "__main__"', 0.95))

        if name == "__main__.py":
            add(EntryPoint("app", info.path, 1, "package __main__", 0.9))
        elif name == "manage.py":
            add(EntryPoint("app", info.path, 1, "Django manage.py", 0.85))
        elif name == "setup.py" and "entry_points" in text:
            add(EntryPoint("cli", info.path, 1, "setup.py entry_points", 0.7))

        server_call = PY_SERVER_CALL.search(text)
        uvicorn = UVICORN_RUN.search(text)
        if (server_call or uvicorn) and (PY_SERVER_IMPORT.search(text) or uvicorn):
            if server_call:
                line = text.count("\n", 0, server_call.start()) + 1
                detail = f"{server_call.group(1)} app"
            else:
                line = text.count("\n", 0, uvicorn.start()) + 1
                detail = "uvicorn.run"
            add(EntryPoint("server", info.path, line, detail, 0.8))

        if info.shebang:
            add(EntryPoint("script", info.path, 1, info.shebang, 0.5))

    for info in files:
        if is_test_path(info.path):
            continue
        path = root / info.path
        if info.language == "Go":
            text = _read_text(path)
            line = _first_line(GO_MAIN, text)
            if line and GO_PACKAGE_MAIN.search(text):
                add(EntryPoint("binary", info.path, line, "func main", 0.95))
        elif info.language == "Rust":
            if Path(info.path).name == "main.rs" or Path(info.path).parts[:1] == ("src",):
                text = _read_text(path)
                line = _first_line(RUST_MAIN, text)
                if line:
                    add(EntryPoint("binary", info.path, line, "fn main", 0.95))
        elif info.language in {"C", "C++"}:
            if Path(info.path).stem.lower() == "main":
                text = _read_text(path)
                line = _first_line(C_MAIN, text)
                if line:
                    add(EntryPoint("binary", info.path, line, "int main", 0.9))
        elif info.language == "Java":
            text = _read_text(path)
            line = _first_line(JAVA_MAIN, text)
            if line:
                add(EntryPoint("binary", info.path, line, "public static void main", 0.9))
        elif info.language == "C#":
            text = _read_text(path)
            line = _first_line(CS_MAIN, text)
            if line:
                add(EntryPoint("binary", info.path, line, "static Main", 0.85))
        elif info.language == "Kotlin":
            text = _read_text(path)
            line = _first_line(KT_MAIN, text)
            if line:
                add(EntryPoint("binary", info.path, line, "fun main", 0.85))
        elif info.language in {"Shell", "JavaScript", "TypeScript", "Ruby", "PHP"}:
            if info.shebang and not is_test_path(info.path):
                add(EntryPoint("script", info.path, 1, info.shebang, 0.55))

    for package_json in (root / "package.json",):
        if not package_json.is_file():
            continue
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        bin_field = data.get("bin")
        if isinstance(bin_field, str):
            bin_field = {data.get("name", "cli"): bin_field}
        if isinstance(bin_field, dict):
            for name, target in bin_field.items():
                target_path = (package_json.parent / str(target)).resolve()
                try:
                    rel = target_path.relative_to(root.resolve()).as_posix()
                except ValueError:
                    continue
                if target_path.is_file():
                    add(EntryPoint("cli", rel, 1, f"bin: {name}", 0.9))
        scripts = data.get("scripts") or {}
        if isinstance(scripts, dict):
            for key in ("start", "dev", "serve"):
                command = scripts.get(key)
                if isinstance(command, str):
                    add(
                        EntryPoint(
                            "server",
                            "package.json",
                            1,
                            f"scripts.{key}: {command}",
                            0.6,
                        )
                    )
                    break

    return sorted(found.values(), key=lambda e: (-e.score, e.path, e.line))
