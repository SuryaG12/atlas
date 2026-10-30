"""SQLite graph and retrieval index.

M2 adds symbol-shaped chunks plus an FTS5 index over names, paths, code, and
docstrings. The graph is still rebuilt from scratch on every ``atlas index``;
incremental updates arrive in M4.
"""

from __future__ import annotations

import posixpath
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from .models import ImportRef, ParsedFile
from .parse import parse_file, tree_sitter_available
from .walker import ScanResult

GRAPH_DIR = ".atlas"
GRAPH_NAME = "graph.db"
SCHEMA_VERSION = 2
MAX_CHUNK_LINES = 160

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    language TEXT NOT NULL,
    lines INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    parse_errors INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id),
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    docstring TEXT NOT NULL DEFAULT '',
    parent_id INTEGER REFERENCES symbols(id),
    exported INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY,
    src_id INTEGER REFERENCES symbols(id),
    dst_id INTEGER REFERENCES symbols(id),
    dst_name TEXT NOT NULL,
    receiver TEXT,
    kind TEXT NOT NULL,
    file_id INTEGER NOT NULL REFERENCES files(id),
    line INTEGER NOT NULL,
    resolution TEXT
);
CREATE TABLE imports (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id),
    module TEXT NOT NULL,
    name TEXT,
    alias TEXT,
    level INTEGER NOT NULL DEFAULT 0,
    line INTEGER NOT NULL,
    dst_file_id INTEGER REFERENCES files(id)
);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id),
    symbol_id INTEGER REFERENCES symbols(id),
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    text TEXT NOT NULL,
    docstring TEXT NOT NULL DEFAULT '',
    tokens INTEGER NOT NULL
);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    name, path, text, docstring, tokenize='unicode61'
);
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_qual ON symbols(qualified_name);
CREATE INDEX idx_symbols_file ON symbols(file_id);
CREATE INDEX idx_edges_src ON edges(src_id);
CREATE INDEX idx_edges_dst ON edges(dst_id);
CREATE INDEX idx_edges_name ON edges(dst_name);
CREATE INDEX idx_imports_file ON imports(file_id);
CREATE INDEX idx_imports_dst ON imports(dst_file_id);
CREATE INDEX idx_chunks_symbol ON chunks(symbol_id);
CREATE INDEX idx_chunks_file ON chunks(file_id);
CREATE INDEX idx_chunks_path ON chunks(path);
"""


@dataclass
class GraphStats:
    files: int = 0
    parsed: int = 0
    symbols: int = 0
    chunks: int = 0
    edges: int = 0
    resolved: int = 0
    internal_edges: int = 0
    imports: int = 0
    parse_errors: int = 0
    kinds: dict[str, int] = field(default_factory=dict)
    top_symbols: list[tuple[str, int]] = field(default_factory=list)
    top_files: list[tuple[str, int]] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def resolved_pct(self) -> float:
        return self.resolved / self.internal_edges * 100 if self.internal_edges else 0.0


def graph_path(root: Path) -> Path:
    return Path(root) / GRAPH_DIR / GRAPH_NAME


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _insert_parsed(
    conn: sqlite3.Connection, file_id: int, parsed: ParsedFile
) -> dict[str, int]:
    ids: dict[str, int] = {}
    pending: list[tuple[str, int]] = []
    for symbol in parsed.symbols:
        cursor = conn.execute(
            "INSERT INTO symbols (file_id, name, qualified_name, kind, start_line,"
            " end_line, signature, docstring, parent_id, exported)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                file_id,
                symbol.name,
                symbol.qualified_name,
                symbol.kind,
                symbol.start_line,
                symbol.end_line,
                symbol.signature,
                symbol.docstring,
                None,
                int(symbol.exported),
            ),
        )
        symbol_id = int(cursor.lastrowid)
        ids.setdefault(symbol.qualified_name, symbol_id)
        if symbol.parent:
            pending.append((symbol.parent, symbol_id))
    for parent_qual, symbol_id in pending:
        parent_id = ids.get(parent_qual)
        if parent_id is not None:
            conn.execute("UPDATE symbols SET parent_id=? WHERE id=?", (parent_id, symbol_id))

    for import_ref in parsed.imports:
        conn.execute(
            "INSERT INTO imports (file_id, module, name, alias, level, line)"
            " VALUES (?,?,?,?,?,?)",
            (
                file_id,
                import_ref.module,
                import_ref.name,
                import_ref.alias,
                import_ref.level,
                import_ref.line,
            ),
        )

    for call in parsed.calls:
        src_id = ids.get(call.enclosing) if call.enclosing else None
        conn.execute(
            "INSERT INTO edges (src_id, dst_id, dst_name, receiver, kind, file_id, line)"
            " VALUES (?,?,?,?,?,?,?)",
            (src_id, None, call.name, call.receiver, "call", file_id, call.line),
        )

    for base, enclosing, line in parsed.inherits:
        src_id = ids.get(enclosing) if enclosing else None
        conn.execute(
            "INSERT INTO edges (src_id, dst_id, dst_name, receiver, kind, file_id, line)"
            " VALUES (?,?,?,?,?,?,?)",
            (src_id, None, base, None, "inherit", file_id, line),
        )

    return ids


def _insert_chunks(
    conn: sqlite3.Connection,
    file_id: int,
    info,
    parsed: ParsedFile,
    symbol_ids: dict[str, int],
    root: Path,
) -> int:
    if not parsed.symbols:
        return 0
    try:
        source = (root / info.path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    lines = source.splitlines()
    if not lines:
        return 0

    inserted = 0
    seen: set[str] = set()
    for symbol in parsed.symbols:
        if symbol.qualified_name in seen:
            continue
        seen.add(symbol.qualified_name)
        start = max(1, symbol.start_line)
        end = min(len(lines), max(symbol.end_line, start))
        if end < start:
            continue
        if end - start + 1 > MAX_CHUNK_LINES:
            end = start + MAX_CHUNK_LINES - 1
        text = "\n".join(lines[start - 1 : end])
        tokens = max(1, len(text) // 4)
        cursor = conn.execute(
            "INSERT INTO chunks (file_id, symbol_id, path, name, kind, start_line,"
            " end_line, text, docstring, tokens) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                file_id,
                symbol_ids.get(symbol.qualified_name),
                info.path,
                symbol.qualified_name,
                symbol.kind,
                start,
                end,
                text,
                symbol.docstring,
                tokens,
            ),
        )
        conn.execute(
            "INSERT INTO chunks_fts (rowid, name, path, text, docstring) VALUES (?,?,?,?,?)",
            (
                int(cursor.lastrowid),
                symbol.qualified_name,
                info.path,
                text,
                symbol.docstring,
            ),
        )
        inserted += 1
    return inserted


def _module_candidates(
    module: str,
    from_path: str,
    language: str,
    level: int,
    all_paths: tuple[str, ...],
) -> list[str]:
    """Best-effort mapping from an import string to repo-relative file paths."""
    candidates: list[str] = []
    if language == "Python":
        parts = module.split(".") if module else []
        if level:
            base = posixpath.dirname(from_path).split("/")
            base = [p for p in base if p]
            drop = level - 1
            if drop:
                base = base[: len(base) - drop] if drop <= len(base) else []
            target_parts = base + parts
        else:
            target_parts = parts
        if not target_parts:
            return []
        stem = "/".join(target_parts)
        candidates.extend([f"{stem}.py", f"{stem}/__init__.py"])
        if stem.rsplit("/", 1)[-1] == "__init__":
            candidates.append(f"{stem}.py")
    else:
        if not module.startswith("."):
            stem = module.rstrip("/")
            suffix = posixpath.basename(stem)
            if suffix:
                candidates.extend(
                    p
                    for p in all_paths
                    if f"/{suffix}/" in f"/{p}" or f"/{suffix}." in f"/{p}"
                )
            return candidates
        base = posixpath.dirname(from_path)
        joint = posixpath.normpath(posixpath.join(base, module)) if base else module
        joint = joint.lstrip("./")
        if language == "Go":
            candidates.extend(p for p in all_paths if p.startswith(joint + "/"))
        else:
            for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
                candidates.append(joint + suffix)
            for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
                candidates.append(f"{joint}/index{suffix}")

    return [c for c in candidates if c in all_paths]


def _resolve_imports(conn: sqlite3.Connection) -> None:
    file_rows = conn.execute("SELECT id, path, language FROM files").fetchall()
    files = {row[0]: row[1] for row in file_rows}
    languages = {row[0]: row[2] for row in file_rows}
    id_by_path = {row[1]: row[0] for row in file_rows}
    all_paths = tuple(sorted(files.values()))

    updates: list[tuple[int, int]] = []
    for import_id, file_id, module, level in conn.execute(
        "SELECT id, file_id, module, level FROM imports WHERE dst_file_id IS NULL"
    ):
        candidates = _module_candidates(
            module, files.get(file_id, ""), languages.get(file_id, ""), level, all_paths
        )
        for candidate in candidates:
            target = id_by_path.get(candidate)
            if target is not None:
                updates.append((target, import_id))
                break
    conn.executemany("UPDATE imports SET dst_file_id=? WHERE id=?", updates)


def _resolve_edges(conn: sqlite3.Connection) -> tuple[int, int, int]:
    file_rows = conn.execute("SELECT id, path, language FROM files").fetchall()
    files = {row[0]: row[1] for row in file_rows}
    languages = {row[0]: row[2] for row in file_rows}
    id_by_path = {row[1]: row[0] for row in file_rows}
    all_paths = tuple(sorted(files.values(), reverse=True))

    rows = conn.execute(
        "SELECT id, file_id, name, qualified_name, kind, parent_id FROM symbols"
    ).fetchall()
    by_qual = {row[3]: row[0] for row in rows}
    by_file_name: dict[tuple[int, str], list[int]] = {}
    by_name: dict[str, list[int]] = {}
    symbol_row = {row[0]: row for row in rows}
    for symbol_id, file_id, name, _qual, _kind, _parent in rows:
        by_file_name.setdefault((file_id, name), []).append(symbol_id)
        by_name.setdefault(name, []).append(symbol_id)

    imports_by_file: dict[int, list[ImportRef]] = {}
    for file_id, module, name, alias, level in conn.execute(
        "SELECT file_id, module, name, alias, level FROM imports"
    ):
        imports_by_file.setdefault(file_id, []).append(
            ImportRef(module=module, name=name, alias=alias, level=level)
        )

    def local_name(imp: ImportRef) -> str | None:
        return imp.alias or imp.name

    def pick_same_file(file_id: int, dst_name: str, src_id: int | None) -> int | None:
        ids = by_file_name.get((file_id, dst_name))
        if not ids:
            return None
        if src_id is not None:
            parent = symbol_row.get(src_id, (None,) * 6)[5]
            if parent is not None:
                parent_qual = symbol_row.get(parent, (None, None, None, None, None, None))[3]
                candidate = by_qual.get(f"{parent_qual}.{dst_name}")
                if candidate is not None:
                    return candidate
        return ids[0]

    pending = conn.execute(
        "SELECT id, src_id, dst_name, receiver, kind, file_id FROM edges WHERE dst_id IS NULL"
    ).fetchall()
    updates: list[tuple[int, str, int]] = []

    for edge_id, src_id, dst_name, receiver, kind, file_id in pending:
        target: int | None = None
        resolution = ""

        if receiver in ("self", "this") and src_id is not None:
            parent_id = symbol_row.get(src_id, (None,) * 6)[5]
            if parent_id is not None:
                parent_qual = symbol_row[parent_id][3]
                target = by_qual.get(f"{parent_qual}.{dst_name}")
                resolution = "self"

        if target is None:
            target = pick_same_file(file_id, dst_name, src_id)
            resolution = "local" if target else ""

        if target is None and kind == "call":
            for imp in imports_by_file.get(file_id, []):
                if local_name(imp) != dst_name:
                    continue
                for candidate_path in _module_candidates(
                    imp.module,
                    files[file_id],
                    languages.get(file_id, ""),
                    imp.level,
                    all_paths,
                ):
                    candidate_file = id_by_path.get(candidate_path)
                    if candidate_file is None:
                        continue
                    ids = by_file_name.get((candidate_file, dst_name))
                    if ids:
                        target = ids[0]
                        resolution = "import"
                        break
                if target is not None:
                    break

        if target is None:
            ids = by_name.get(dst_name, [])
            if len(ids) == 1:
                candidate_id = ids[0]
                candidate_parent = symbol_row[candidate_id][5]
                if receiver in (None, "self", "this"):
                    target = candidate_id
                    resolution = "unique"
                elif candidate_parent is not None:
                    parent_name = symbol_row[candidate_parent][2]
                    if receiver.split(".")[-1] == parent_name:
                        target = candidate_id
                        resolution = "receiver"

        if target is not None:
            updates.append((target, resolution or "unique", edge_id))

    conn.executemany("UPDATE edges SET dst_id=?, resolution=? WHERE id=?", updates)
    unresolved_internal = conn.execute(
        "SELECT COUNT(*) FROM edges e WHERE e.dst_id IS NULL"
        " AND EXISTS (SELECT 1 FROM symbols s WHERE s.name = e.dst_name)"
    ).fetchone()[0]
    return len(pending), len(updates), len(updates) + unresolved_internal


def build_graph(
    root: Path, scan_result: ScanResult, db_path: Path | None = None
) -> GraphStats:
    root = Path(root)
    target = db_path or graph_path(root)
    started = perf_counter()
    if target.exists():
        target.unlink()
    for suffix in ("-wal", "-shm"):
        leftover = target.with_name(target.name + suffix)
        if leftover.exists():
            leftover.unlink()

    conn = _connect(target)
    stats = GraphStats()
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema', ?), ('tool', 'atlas')",
            (str(SCHEMA_VERSION),),
        )

        for info in scan_result.files:
            cursor = conn.execute(
                "INSERT INTO files (path, language, lines, bytes, sha256, parse_errors)"
                " VALUES (?,?,?,?,?,0)",
                (info.path, info.language, info.lines, info.bytes, info.sha256),
            )
            file_id = int(cursor.lastrowid)
            stats.files += 1

            parsed = parse_file(root, info)
            if parsed is None:
                continue
            stats.parsed += 1
            if parsed.errors:
                stats.parse_errors += 1
                conn.execute(
                    "UPDATE files SET parse_errors=? WHERE id=?", (parsed.errors, file_id)
                )
            symbol_ids = _insert_parsed(conn, file_id, parsed)
            stats.chunks += _insert_chunks(conn, file_id, info, parsed, symbol_ids, root)

        _resolve_imports(conn)
        pending, resolved, internal = _resolve_edges(conn)
        conn.commit()

        stats.edges = pending
        stats.resolved = resolved
        stats.internal_edges = internal
        stats.symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        stats.chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        stats.imports = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
        stats.kinds = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT kind, COUNT(*) FROM symbols GROUP BY kind ORDER BY COUNT(*) DESC"
            )
        }
        stats.top_symbols = [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT s.qualified_name, COUNT(*) c FROM edges e"
                " JOIN symbols s ON s.id = e.dst_id"
                " GROUP BY e.dst_id ORDER BY c DESC, s.qualified_name LIMIT 8"
            )
        ]
        stats.top_files = [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT f.path, COUNT(*) c FROM edges e"
                " JOIN symbols s ON s.id = e.dst_id"
                " JOIN files f ON f.id = s.file_id"
                " GROUP BY f.path ORDER BY c DESC, f.path LIMIT 8"
            )
        ]
    finally:
        conn.close()

    stats.seconds = perf_counter() - started
    return stats


def open_graph(root: Path) -> sqlite3.Connection:
    path = graph_path(Path(root))
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def graph_schema(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.DatabaseError:
        return 0


def tree_sitter_status() -> bool:
    return tree_sitter_available()
