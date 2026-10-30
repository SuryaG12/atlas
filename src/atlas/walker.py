"""Repository walker: gitignore-aware, binary-safe, hash-everything."""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from .ignore import IgnoreRules
from .langs import detect_language, is_code

BINARY_SNIFF_BYTES = 8192
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024

SHEBANGS = {
    "python": "Python",
    "python3": "Python",
    "node": "JavaScript",
    "deno": "TypeScript",
    "bash": "Shell",
    "sh": "Shell",
    "zsh": "Shell",
    "ruby": "Ruby",
    "php": "PHP",
    "perl": "Other",
}


@dataclass(slots=True)
class FileInfo:
    path: str  # posix, relative to the repo root
    language: str
    lines: int
    bytes: int
    sha256: str
    shebang: str | None = None

    @property
    def is_code(self) -> bool:
        return is_code(self.language)


@dataclass
class ScanResult:
    root: Path
    files: list[FileInfo] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    duration_s: float = 0.0
    gitignores: int = 0

    @property
    def total_bytes(self) -> int:
        return sum(f.bytes for f in self.files)

    @property
    def total_lines(self) -> int:
        return sum(f.lines for f in self.files)

    @property
    def code_lines(self) -> int:
        return sum(f.lines for f in self.files if f.is_code)


def _count_lines(data: bytes) -> int:
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _shebang_language(first_line: bytes) -> tuple[str | None, str | None]:
    if not first_line.startswith(b"#!"):
        return None, None
    text = first_line.decode("utf-8", errors="replace").strip()
    parts = text[2:].strip().split()
    if not parts:
        return None, text
    interpreter = Path(parts[0]).name
    if interpreter == "env" and len(parts) > 1:
        interpreter = parts[1]
    return SHEBANGS.get(interpreter), text


def _inspect(path: Path, root: Path, max_bytes: int) -> tuple[str, FileInfo | None]:
    try:
        stat = path.stat()
    except OSError:
        return "unreadable", None
    if stat.st_size > max_bytes:
        return "large", None
    if stat.st_size == 0:
        return "empty", None
    try:
        data = path.read_bytes()
    except OSError:
        return "unreadable", None
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        return "binary", None

    first_line = data.split(b"\n", 1)[0]
    shebang_lang, shebang = _shebang_language(first_line)
    language = detect_language(path)
    if language == "Other" and shebang_lang:
        language = shebang_lang

    return (
        "ok",
        FileInfo(
            path=path.relative_to(root).as_posix(),
            language=language,
            lines=_count_lines(data),
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            shebang=shebang,
        ),
    )


def scan(
    root: Path | str,
    *,
    excludes: tuple[str, ...] = (),
    include_vendored: bool = False,
    use_gitignore: bool = True,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> ScanResult:
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)

    started = perf_counter()
    rules = IgnoreRules(
        root,
        excludes=excludes,
        include_vendored=include_vendored,
        use_gitignore=use_gitignore,
    )
    result = ScanResult(root=root)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        directory = Path(dirpath)
        rules.load_gitignore(directory)

        keep: list[str] = []
        for name in sorted(dirnames):
            child = directory / name
            if child.is_symlink():
                result.skipped["symlink"] += 1
                continue
            if rules.is_ignored(child, is_dir=True):
                result.skipped["ignored_dir"] += 1
                continue
            keep.append(name)
        dirnames[:] = keep

        for name in sorted(filenames):
            child = directory / name
            if child.is_symlink():
                result.skipped["symlink"] += 1
                continue
            if rules.is_ignored(child, is_dir=False):
                result.skipped["ignored_file"] += 1
                continue
            status, info = _inspect(child, root, max_file_bytes)
            if status != "ok" or info is None:
                result.skipped[status] += 1
                continue
            result.files.append(info)

    result.files.sort(key=lambda f: f.path)
    result.gitignores = rules.gitignore_count
    result.duration_s = perf_counter() - started
    return result
