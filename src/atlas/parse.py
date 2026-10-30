"""Parser dispatch."""

from __future__ import annotations

from pathlib import Path

from .models import ParsedFile
from .walker import FileInfo

TREE_SITTER_LANGUAGES = {"JavaScript", "TypeScript", "Go"}


def _tree_sitter_grammar(info: FileInfo) -> str:
    suffix = Path(info.path).suffix.lower()
    if info.language == "TypeScript":
        return "TSX" if suffix == ".tsx" else "TypeScript"
    if info.language == "JavaScript":
        return "JavaScript"
    return "Go"


def tree_sitter_available() -> bool:
    from . import parser_treesitter

    return parser_treesitter.AVAILABLE


def parse_file(root: Path, info: FileInfo) -> ParsedFile | None:
    root = Path(root)
    if info.language == "Python":
        from .parser_python import parse_python

        return parse_python(root / info.path, info.path)

    if info.language in TREE_SITTER_LANGUAGES:
        from . import parser_treesitter

        if not parser_treesitter.AVAILABLE:
            return None
        return parser_treesitter.parse_treesitter(
            root / info.path, info.path, _tree_sitter_grammar(info)
        )

    return None
