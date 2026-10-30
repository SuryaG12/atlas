"""M4: diff the working tree against the last indexed state.

Read-only by construction: the graph is opened with ``mode=ro`` and the
manifest is only read. Nothing is written, so ``atlas diff`` can never change
what the next index sees.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3

from .graph import SCHEMA_VERSION, graph_schema, open_graph
from .manifest import manifest_shas, read_manifest
from .parse import parse_file
from .walker import ScanResult, scan

NO_INDEX = "No index found. Run `atlas index .` first."
STALE_INDEX = "Graph index is out of date. Run `atlas index .` again."

SymbolKey = tuple[str, str, int, int, str, str, str]


class NoIndexError(RuntimeError):
    """Raised when there is no usable manifest/graph to diff against."""


@dataclass(frozen=True, order=True)
class FileChange:
    path: str
    old_sha: str
    new_sha: str


@dataclass(frozen=True, order=True)
class SymbolChange:
    path: str
    line: int
    name: str
    kind: str
    old_line: int | None = None


@dataclass(frozen=True, order=True)
class EdgeCount:
    path: str
    count: int


@dataclass
class DiffResult:
    root: Path
    files_added: list[str] = field(default_factory=list)
    files_deleted: list[str] = field(default_factory=list)
    files_changed: list[FileChange] = field(default_factory=list)
    symbols_added: list[SymbolChange] = field(default_factory=list)
    symbols_removed: list[SymbolChange] = field(default_factory=list)
    symbols_changed: list[SymbolChange] = field(default_factory=list)
    edges_added: list[EdgeCount] = field(default_factory=list)
    edges_removed: list[EdgeCount] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not any(
            (
                self.files_added,
                self.files_deleted,
                self.files_changed,
                self.symbols_added,
                self.symbols_removed,
                self.symbols_changed,
                self.edges_added,
                self.edges_removed,
            )
        )


def _old_symbols(conn: sqlite3.Connection) -> dict[str, dict[str, SymbolKey]]:
    rows = conn.execute(
        "SELECT f.path, s.qualified_name, s.name, s.kind, s.start_line,"
        " s.end_line, s.signature, s.docstring"
        " FROM symbols s JOIN files f ON f.id = s.file_id"
    )
    result: dict[str, dict[str, SymbolKey]] = {}
    for path, qual, name, kind, start, end, signature, docstring in rows:
        result.setdefault(path, {}).setdefault(
            qual, (name, kind, start, end, signature, docstring, qual)
        )
    return result


def _old_edges(conn: sqlite3.Connection) -> Counter[tuple[str, str, str, str, int, str]]:
    rows = conn.execute(
        "SELECT f.path, e.kind, e.dst_name, COALESCE(e.receiver, ''), e.line,"
        " COALESCE(src.qualified_name, '')"
        " FROM edges e"
        " JOIN files f ON f.id = e.file_id"
        " LEFT JOIN symbols src ON src.id = e.src_id"
    )
    return Counter(tuple(row) for row in rows)


def _current_edges(parsed) -> Counter[tuple[str, str, str, str, int, str]]:
    edges: Counter[tuple[str, str, str, str, int, str]] = Counter()
    if parsed is None:
        return edges
    for call in parsed.calls:
        edges[
            (
                parsed.path,
                "call",
                call.name,
                call.receiver or "",
                call.line,
                call.enclosing or "",
            )
        ] += 1
    for base, enclosing, line in parsed.inherits:
        edges[(parsed.path, "inherit", base, "", line, enclosing or "")] += 1
    return edges


def _summarize(counter: Counter[tuple[str, str, str, str, int, str]]) -> list[EdgeCount]:
    by_path: Counter[str] = Counter()
    for (path, _kind, _dst, _recv, _line, _src), count in counter.items():
        by_path[path] += count
    return [EdgeCount(path=path, count=count) for path, count in sorted(by_path.items()) if count]


def compute_diff(root: Path, scan_result: ScanResult | None = None) -> DiffResult:
    """Compare the working tree with the manifest and graph. Never writes."""
    root = Path(root)
    manifest = read_manifest(root)
    old_shas = manifest_shas(manifest)
    if old_shas is None:
        raise NoIndexError(NO_INDEX)

    scanned = scan_result or scan(root)
    new_files = {info.path: info for info in scanned.files}

    try:
        conn = open_graph(root)
    except FileNotFoundError as exc:
        raise NoIndexError(NO_INDEX) from exc
    try:
        if graph_schema(conn) != SCHEMA_VERSION:
            raise NoIndexError(STALE_INDEX)
        old_symbols = _old_symbols(conn)
        old_edges = _old_edges(conn)
    finally:
        conn.close()

    added = sorted(path for path in new_files if path not in old_shas)
    deleted = sorted(path for path in old_shas if path not in new_files)
    changed = sorted(
        path
        for path, info in new_files.items()
        if path in old_shas and old_shas[path] != info.sha256
    )

    result = DiffResult(
        root=root,
        files_added=added,
        files_deleted=deleted,
        files_changed=[
            FileChange(path=path, old_sha=old_shas[path], new_sha=new_files[path].sha256)
            for path in changed
        ],
    )

    symbols_added: list[SymbolChange] = []
    symbols_removed: list[SymbolChange] = []
    symbols_changed: list[SymbolChange] = []

    for path in deleted:
        for name, _kind, start, _end, _sig, _doc, _qual in old_symbols.get(path, {}).values():
            symbols_removed.append(
                SymbolChange(path=path, line=start, name=name, kind=_kind)
            )

    scope = set(added) | set(changed)
    new_edges: Counter[tuple[str, str, str, str, int, str]] = Counter()
    for path in sorted(scope):
        info = new_files[path]
        parsed = parse_file(root, info)
        current = {
            symbol.qualified_name: (
                symbol.name,
                symbol.kind,
                symbol.start_line,
                symbol.end_line,
                symbol.signature,
                symbol.docstring,
                symbol.qualified_name,
            )
            for symbol in (parsed.symbols if parsed else [])
        }
        previous = old_symbols.get(path, {})

        for qual in sorted(current.keys() - previous.keys()):
            name, kind, start, _end, _sig, _doc, _q = current[qual]
            symbols_added.append(SymbolChange(path=path, line=start, name=name, kind=kind))
        for qual in sorted(previous.keys() - current.keys()):
            name, kind, start, _end, _sig, _doc, _q = previous[qual]
            symbols_removed.append(SymbolChange(path=path, line=start, name=name, kind=kind))
        for qual in sorted(current.keys() & previous.keys()):
            name, kind, start, _end, _sig, _doc, _q = current[qual]
            old_name, old_kind, old_start, _oe, _os, _od, _oq = previous[qual]
            if (name, kind, start, _end, _sig, _doc) != (
                old_name,
                old_kind,
                old_start,
                _oe,
                _os,
                _od,
            ):
                symbols_changed.append(
                    SymbolChange(
                        path=path,
                        line=start,
                        name=name,
                        kind=kind,
                        old_line=old_start,
                    )
                )

        new_edges.update(_current_edges(parsed))

    result.symbols_added = sorted(symbols_added)
    result.symbols_removed = sorted(symbols_removed)
    result.symbols_changed = sorted(symbols_changed)

    edge_scope = set(added) | set(changed) | set(deleted)
    old_scoped = Counter(
        {key: count for key, count in old_edges.items() if key[0] in edge_scope}
    )
    new_scoped = Counter(
        {key: count for key, count in new_edges.items() if key[0] in edge_scope}
    )
    result.edges_added = _summarize(new_scoped - old_scoped)
    result.edges_removed = _summarize(old_scoped - new_scoped)
    return result


def to_dict(result: DiffResult) -> dict:
    """Machine-readable form; same groups as the text output."""
    return {
        "root": str(result.root),
        "clean": result.clean,
        "files": {
            "added": list(result.files_added),
            "deleted": list(result.files_deleted),
            "changed": [
                {
                    "path": change.path,
                    "old_sha256": change.old_sha,
                    "new_sha256": change.new_sha,
                }
                for change in result.files_changed
            ],
        },
        "symbols": {
            "added": [
                {"path": s.path, "line": s.line, "name": s.name, "kind": s.kind}
                for s in result.symbols_added
            ],
            "removed": [
                {"path": s.path, "line": s.line, "name": s.name, "kind": s.kind}
                for s in result.symbols_removed
            ],
            "changed": [
                {
                    "path": s.path,
                    "line": s.line,
                    "name": s.name,
                    "kind": s.kind,
                    "old_line": s.old_line,
                }
                for s in result.symbols_changed
            ],
        },
        "edges": {
            "added": [{"path": e.path, "count": e.count} for e in result.edges_added],
            "removed": [{"path": e.path, "count": e.count} for e in result.edges_removed],
        },
    }


def _short(sha: str) -> str:
    return sha[:12]


def render_text(result: DiffResult) -> str:
    lines = [f"Atlas diff  {result.root}", ""]
    if result.clean:
        lines.append("No changes. The working tree matches the last indexed state.")
        return "\n".join(lines)

    lines.append(f"Files")
    lines.append(f"  added ({len(result.files_added)})")
    lines.extend(f"    {path}" for path in result.files_added)
    lines.append(f"  deleted ({len(result.files_deleted)})")
    lines.extend(f"    {path}" for path in result.files_deleted)
    lines.append(f"  changed ({len(result.files_changed)})")
    lines.extend(
        f"    {change.path}  {_short(change.old_sha)} -> {_short(change.new_sha)}"
        for change in result.files_changed
    )

    lines.append("")
    lines.append("Symbols")
    lines.append(f"  added ({len(result.symbols_added)})")
    lines.extend(
        f"    {s.path}:{s.line}  {s.name} ({s.kind})" for s in result.symbols_added
    )
    lines.append(f"  removed ({len(result.symbols_removed)})")
    lines.extend(
        f"    {s.path}:{s.line}  {s.name} ({s.kind})" for s in result.symbols_removed
    )
    lines.append(f"  changed ({len(result.symbols_changed)})")
    lines.extend(
        f"    {s.path}:{s.line}  {s.name} ({s.kind})"
        + (f"  was line {s.old_line}" if s.old_line is not None and s.old_line != s.line else "")
        for s in result.symbols_changed
    )

    lines.append("")
    lines.append("Edges")
    lines.append(f"  added ({len(result.edges_added)} file(s))")
    lines.extend(f"    {e.path}  +{e.count}" for e in result.edges_added)
    lines.append(f"  removed ({len(result.edges_removed)} file(s))")
    lines.extend(f"    {e.path}  -{e.count}" for e in result.edges_removed)
    return "\n".join(lines)
