"""Hybrid retrieval: symbol lookup, FTS5, one-hop graph expansion, RRF fusion.

No embeddings in M2. The three sources are deliberately complementary:

* symbol lookup  - exact / prefix identifier matches, high precision
* FTS5           - names, paths, code, docstrings, catches prose and comments
* graph expansion - callers, callees, subclasses, importers of the top hits,
                    which is the part embeddings cannot give you
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

RRF_K = 60
MAX_TOKENS = 12
SIBLING_SIGNATURES = 6
SIBLING_BUDGET = 400

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "that", "this",
    "these", "those", "is", "are", "was", "were", "be", "been", "being", "do",
    "does", "did", "doing", "have", "has", "had", "having", "can", "could",
    "should", "would", "will", "shall", "may", "might", "must", "to", "of",
    "in", "on", "at", "by", "for", "with", "from", "into", "onto", "over",
    "under", "about", "as", "it", "its", "they", "them", "their", "there",
    "here", "where", "when", "why", "what", "which", "who", "whom", "how",
    "i", "we", "you", "me", "my", "our", "your", "us", "not", "no", "yes",
    "code", "codebase", "repo", "repository", "project", "file", "files",
    "function", "functions", "method", "methods", "class", "classes", "module",
    "modules", "work", "works", "working", "handle", "handles", "handled",
    "handling", "implement", "implements", "implemented", "implementation",
    "find", "show", "tell", "explain", "describe", "get", "gets", "got",
    "make", "makes", "made", "use", "uses", "used", "using", "call", "calls",
    "called", "run", "runs", "return", "returns", "defined", "definition",
    "some", "any", "all", "one", "two", "new", "old", "main", "thing", "things",
    "happen", "happens", "way", "works", "please", "like", "look", "looks",
}


@dataclass(slots=True)
class RetrievalChunk:
    chunk_id: int
    symbol_id: int | None
    path: str
    name: str
    kind: str
    start: int
    end: int
    text: str
    docstring: str
    tokens: int
    calls: list[str] = field(default_factory=list)
    siblings: list[str] = field(default_factory=list)
    score: float = 0.0
    ranks: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "tokens": self.tokens,
            "score": round(self.score, 5),
            "ranks": dict(self.ranks),
            "calls": self.calls,
            "siblings": self.siblings,
        }


@dataclass(slots=True)
class SourceHit:
    rank: int
    chunk_id: int
    path: str
    name: str
    start: int
    end: int
    raw: float | None = None

    def summary(self) -> dict:
        payload = {
            "rank": self.rank,
            "chunk_id": self.chunk_id,
            "path": self.path,
            "name": self.name,
            "start": self.start,
            "end": self.end,
        }
        if self.raw is not None:
            payload["raw"] = round(self.raw, 5)
        return payload


@dataclass
class RetrievalResult:
    question: str
    tokens: list[str]
    chunks: list[RetrievalChunk] = field(default_factory=list)
    fused: list[tuple[int, float, dict[str, int]]] = field(default_factory=list)
    sources: dict[str, list[SourceHit]] = field(default_factory=dict)
    best_score: float = 0.0
    refused: bool = False
    reason: str = ""
    exact_symbol_match: bool = False
    budget_used: int = 0

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "tokens": self.tokens,
            "refused": self.refused,
            "reason": self.reason,
            "best_score": round(self.best_score, 5),
            "exact_symbol_match": self.exact_symbol_match,
            "budget_used": self.budget_used,
            "chunks": [chunk.summary() for chunk in self.chunks],
            "sources": {
                name: [hit.summary() for hit in hits]
                for name, hits in self.sources.items()
            },
            "fused": [
                {"chunk_id": cid, "score": round(score, 5), "ranks": ranks}
                for cid, score, ranks in self.fused
            ],
        }


def extract_tokens(question: str) -> list[str]:
    tokens: list[str] = []
    for raw in TOKEN_RE.findall(question):
        variants = [raw]
        if "_" in raw:
            variants.extend(part for part in raw.split("_") if part)
        variants.extend(CAMEL_RE.split(raw))
        for index, variant in enumerate(variants):
            if index:
                variant = variant.strip("_")
            if len(variant) < 2 or variant.lower() in STOPWORDS:
                continue
            if variant not in tokens:
                tokens.append(variant)
            if len(tokens) >= MAX_TOKENS:
                return tokens
    return tokens


def symbol_lookup(
    conn: sqlite3.Connection, tokens: list[str], limit: int = 30
) -> tuple[list[int], set[int]]:
    ordered: list[int] = []
    exact: set[int] = set()
    seen: set[int] = set()

    def add(chunk_id: int, is_exact: bool) -> None:
        if chunk_id in seen:
            return
        seen.add(chunk_id)
        ordered.append(chunk_id)
        if is_exact:
            exact.add(chunk_id)

    for token in tokens[:10]:
        rows = conn.execute(
            "SELECT c.id FROM symbols s JOIN chunks c ON c.symbol_id = s.id"
            " WHERE s.name = ? COLLATE NOCASE"
            " ORDER BY length(s.qualified_name), s.id LIMIT 5",
            (token,),
        ).fetchall()
        for row in rows:
            add(row[0], True)

        rows = conn.execute(
            "SELECT c.id FROM symbols s JOIN chunks c ON c.symbol_id = s.id"
            " WHERE s.name LIKE ? COLLATE NOCASE OR s.qualified_name LIKE ? COLLATE NOCASE"
            " ORDER BY (s.name LIKE ? COLLATE NOCASE) DESC, length(s.qualified_name), s.id"
            " LIMIT 10",
            (token + "%", "%" + token + "%", token + "%"),
        ).fetchall()
        for row in rows:
            add(row[0], False)

        if len(ordered) >= limit:
            break

    return ordered[:limit], exact


def fts_search(
    conn: sqlite3.Connection, tokens: list[str], limit: int = 30
) -> tuple[list[int], dict[int, float]]:
    if not tokens:
        return [], {}
    query = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens[:10])
    try:
        rows = conn.execute(
            "SELECT rowid, bm25(chunks_fts, 6.0, 1.0, 1.0, 3.0) AS score"
            " FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
            (query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return [], {}
    # bm25 is negative and lower-is-better; flip for human-friendly output
    return [row[0] for row in rows], {row[0]: -float(row[1]) for row in rows}


def _placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


def expand(
    conn: sqlite3.Connection, seed_chunk_ids: list[int], limit: int = 30
) -> list[int]:
    if not seed_chunk_ids:
        return []

    marks = _placeholders(len(seed_chunk_ids))
    seed_symbols = [
        row[0]
        for row in conn.execute(
            f"SELECT DISTINCT symbol_id FROM chunks WHERE id IN ({marks})"
            " AND symbol_id IS NOT NULL",
            seed_chunk_ids,
        )
    ]
    seed_files = [
        row[0]
        for row in conn.execute(
            f"SELECT DISTINCT file_id FROM chunks WHERE id IN ({marks})", seed_chunk_ids
        )
    ]

    ordered: list[int] = []
    seen: set[int] = set(seed_chunk_ids)

    def add(rows) -> None:
        for row in rows:
            chunk_id = row[0]
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            ordered.append(chunk_id)

    if seed_symbols:
        marks = _placeholders(len(seed_symbols))
        add(
            conn.execute(
                f"SELECT c.id FROM edges e JOIN chunks c ON c.symbol_id = e.dst_id"
                f" WHERE e.src_id IN ({marks}) AND e.dst_id IS NOT NULL"
                f" AND e.kind = 'call' ORDER BY e.id",
                seed_symbols,
            )
        )
        add(
            conn.execute(
                f"SELECT c.id FROM edges e JOIN chunks c ON c.symbol_id = e.src_id"
                f" WHERE e.dst_id IN ({marks}) AND e.src_id IS NOT NULL"
                f" AND e.kind = 'call' ORDER BY e.id",
                seed_symbols,
            )
        )
        add(
            conn.execute(
                f"SELECT c.id FROM edges e JOIN chunks c ON c.symbol_id = e.src_id"
                f" WHERE e.dst_id IN ({marks}) AND e.kind = 'inherit' ORDER BY e.id",
                seed_symbols,
            )
        )
        add(
            conn.execute(
                f"SELECT c.id FROM edges e JOIN chunks c ON c.symbol_id = e.dst_id"
                f" WHERE e.src_id IN ({marks}) AND e.kind = 'inherit' ORDER BY e.id",
                seed_symbols,
            )
        )

    if seed_files:
        marks = _placeholders(len(seed_files))
        importers = [
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT file_id FROM imports WHERE dst_file_id IN ({marks})",
                seed_files,
            )
        ]
        if importers:
            marks = _placeholders(len(importers))
            add(
                conn.execute(
                    f"SELECT c.id FROM chunks c JOIN symbols s ON s.id = c.symbol_id"
                    f" WHERE c.file_id IN ({marks}) AND s.parent_id IS NULL"
                    f" ORDER BY c.file_id, c.start_line",
                    importers,
                )
            )

    return ordered[:limit]


def fuse(rankings: dict[str, list[int]], k: int = RRF_K) -> list[tuple[int, float, dict[str, int]]]:
    scores: dict[int, float] = defaultdict(float)
    ranks: dict[int, dict[str, int]] = defaultdict(dict)
    best_rank: dict[int, int] = {}

    for source, ids in rankings.items():
        seen: set[int] = set()
        for rank, chunk_id in enumerate(ids, start=1):
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            scores[chunk_id] += 1.0 / (k + rank)
            ranks[chunk_id][source] = rank
            best_rank[chunk_id] = min(best_rank.get(chunk_id, rank), rank)

    ordered = sorted(
        scores,
        key=lambda cid: (-scores[cid], best_rank[cid], cid),
    )
    return [(cid, scores[cid], ranks[cid]) for cid in ordered]


def _load_chunks(conn: sqlite3.Connection, chunk_ids: list[int]) -> dict[int, sqlite3.Row]:
    if not chunk_ids:
        return {}
    marks = _placeholders(len(chunk_ids))
    rows = conn.execute(
        f"SELECT * FROM chunks WHERE id IN ({marks})", chunk_ids
    ).fetchall()
    return {row["id"]: row for row in rows}


def _calls_for(conn: sqlite3.Connection, symbol_ids: list[int]) -> dict[int, list[str]]:
    if not symbol_ids:
        return {}
    marks = _placeholders(len(symbol_ids))
    grouped: dict[int, list[str]] = defaultdict(list)
    for src_id, name in conn.execute(
        f"SELECT DISTINCT src_id, dst_name FROM edges WHERE kind='call'"
        f" AND src_id IN ({marks}) ORDER BY id",
        symbol_ids,
    ):
        if name not in grouped[src_id]:
            grouped[src_id].append(name)
    return grouped


def _siblings(conn: sqlite3.Connection, chunk: sqlite3.Row) -> list[str]:
    if chunk["symbol_id"] is None:
        return []
    row = conn.execute(
        "SELECT file_id, parent_id, id AS symbol_id FROM symbols WHERE id=?",
        (chunk["symbol_id"],),
    ).fetchone()
    if row is None:
        return []
    parent = row["parent_id"]
    if parent is None:
        rows = conn.execute(
            "SELECT signature FROM symbols WHERE file_id=? AND parent_id IS NULL"
            " AND id!=? ORDER BY start_line LIMIT ?",
            (row["file_id"], row["symbol_id"], SIBLING_SIGNATURES),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT signature FROM symbols WHERE parent_id=? AND id!=?"
            " ORDER BY start_line LIMIT ?",
            (parent, row["symbol_id"], SIBLING_SIGNATURES),
        ).fetchall()
    return [r[0] for r in rows if r[0]]


def retrieve(
    conn: sqlite3.Connection,
    question: str,
    *,
    top_k: int = 8,
    budget: int = 6000,
    min_score: float = 0.02,
) -> RetrievalResult:
    tokens = extract_tokens(question)
    result = RetrievalResult(question=question, tokens=tokens)

    symbol_ids, exact = symbol_lookup(conn, tokens)
    fts_ids, fts_raw = fts_search(conn, tokens)
    seeds = list(dict.fromkeys(symbol_ids[:3] + fts_ids[:3]))[:4]
    graph_ids = expand(conn, seeds)

    rankings = {"symbol": symbol_ids, "fts": fts_ids, "graph": graph_ids}
    hits = fuse(rankings)
    result.fused = hits
    result.exact_symbol_match = bool(exact)

    if not hits:
        result.refused = True
        result.reason = "no symbol or text matches"
        return result

    result.best_score = hits[0][1]
    if result.best_score < min_score and not exact:
        result.refused = True
        result.reason = (
            f"best fused score {result.best_score:.4f} < {min_score:.4f}"
            " and no exact symbol match"
        )
        return result

    rows = _load_chunks(conn, [cid for cid, _score, _ranks in hits[: top_k * 6]])
    symbol_ids_for_calls = [
        row["symbol_id"] for row in rows.values() if row["symbol_id"] is not None
    ]
    calls = _calls_for(conn, symbol_ids_for_calls)

    used = 0
    sibling_tokens = 0
    for chunk_id, score, ranks in hits:
        row = rows.get(chunk_id)
        if row is None:
            continue
        cost = int(row["tokens"]) + 8
        if result.chunks and used + cost > budget:
            break
        if not result.chunks and cost > budget:
            cost = budget
        used += cost
        chunk = RetrievalChunk(
            chunk_id=row["id"],
            symbol_id=row["symbol_id"],
            path=row["path"],
            name=row["name"],
            kind=row["kind"],
            start=row["start_line"],
            end=row["end_line"],
            text=row["text"],
            docstring=row["docstring"],
            tokens=row["tokens"],
            calls=calls.get(row["symbol_id"], [])[:12],
            score=score,
            ranks=dict(ranks),
        )
        if sibling_tokens < SIBLING_BUDGET:
            chunk.siblings = _siblings(conn, row)
            sibling_tokens += 6 * len(chunk.siblings)
        result.chunks.append(chunk)
        if len(result.chunks) >= top_k:
            break

    result.budget_used = used

    meta = _load_chunks(conn, [hit[0] for hit in hits[:10]])
    for name, ids in rankings.items():
        hits_out: list[SourceHit] = []
        for rank, chunk_id in enumerate(ids[:10], start=1):
            row = meta.get(chunk_id)
            if row is None:
                continue
            hits_out.append(
                SourceHit(
                    rank=rank,
                    chunk_id=chunk_id,
                    path=row["path"],
                    name=row["name"],
                    start=row["start_line"],
                    end=row["end_line"],
                    raw=fts_raw.get(chunk_id) if name == "fts" else None,
                )
            )
        result.sources[name] = hits_out

    return result
