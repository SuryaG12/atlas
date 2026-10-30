"""Shared data structures for the parsing layer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SymbolDef:
    name: str
    qualified_name: str
    kind: str  # function | method | class | struct | interface | type | variable
    start_line: int
    end_line: int
    signature: str = ""
    parent: str | None = None
    exported: bool = True
    docstring: str = ""


@dataclass(slots=True)
class ImportRef:
    module: str
    line: int = 0
    name: str | None = None
    alias: str | None = None
    level: int = 0  # relative import depth (Python)


@dataclass(slots=True)
class CallRef:
    name: str
    line: int = 0
    receiver: str | None = None
    enclosing: str | None = None  # qualified name of the enclosing symbol


@dataclass(slots=True)
class ParsedFile:
    path: str
    language: str
    symbols: list[SymbolDef] = field(default_factory=list)
    imports: list[ImportRef] = field(default_factory=list)
    calls: list[CallRef] = field(default_factory=list)
    inherits: list[tuple[str, str | None, int]] = field(default_factory=list)
    # inherits entries: (base name, enclosing qualified name, line)
    errors: int = 0
