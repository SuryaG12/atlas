"""Atlas M3: local-only, read-only web surface.

This is a thin adapter. Retrieval, answering, refusal, citation validation, and
the module map all come from the existing M0-M2 modules; nothing is
reimplemented here. There are no write routes and no way to read outside the
indexed file list.

Binds to 127.0.0.1 only (see ``BIND_HOST`` and ``atlas web``).
"""

from __future__ import annotations

import json
import posixpath
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .answer import REFUSAL, answer
from .graph import SCHEMA_VERSION, graph_schema, open_graph
from .manifest import MANIFEST_DIR, MANIFEST_NAME
from .map import build_map, to_json, to_mermaid
from .narrate import LLMUnavailable, NO_MODEL_MESSAGE, detect_model
from .retrieval import retrieve

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
MAX_SOURCE_LINES = 5000
STATIC_DIR = Path(__file__).with_name("web_static")

NO_GRAPH = "No graph found. Run `atlas index .` first."
STALE_GRAPH = "Graph index is out of date. Run `atlas index .` again."


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=8, ge=1, le=20)
    budget: int = Field(default=6000, ge=200, le=200000)
    min_score: float = Field(default=0.02, ge=0.0, le=1.0)


def _manifest_files(root: Path) -> list[dict] | None:
    path = root / MANIFEST_DIR / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = payload.get("files")
    if not isinstance(entries, list):
        return None
    files: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            continue
        files.append(
            {
                "path": rel,
                "language": entry.get("language") or "Text",
                "lines": int(entry.get("lines") or 0),
                "bytes": int(entry.get("bytes") or 0),
            }
        )
    return files


def _graph_files(conn) -> list[dict]:
    return [
        {
            "path": row["path"],
            "language": row["language"],
            "lines": row["lines"],
            "bytes": row["bytes"],
        }
        for row in conn.execute(
            "SELECT path, language, lines, bytes FROM files ORDER BY path"
        )
    ]


def _file_index(root: Path, conn) -> list[dict]:
    """M0 manifest when present, M1 graph otherwise. Never the live filesystem."""
    files = _manifest_files(root)
    if files is None:
        files = _graph_files(conn)
    return sorted(files, key=lambda entry: entry["path"])


def _graph_counts(conn) -> dict:
    files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    internal = conn.execute(
        "SELECT COUNT(*) FROM edges e"
        " WHERE EXISTS (SELECT 1 FROM symbols s WHERE s.name = e.dst_name)"
    ).fetchone()[0]
    resolved = conn.execute(
        "SELECT COUNT(*) FROM edges e WHERE e.dst_id IS NOT NULL"
        " AND EXISTS (SELECT 1 FROM symbols s WHERE s.name = e.dst_name)"
    ).fetchone()[0]
    return {
        "files": files,
        "symbols": symbols,
        "chunks": chunks,
        "edges": edges,
        "internal_edges": internal,
        "resolved": resolved,
        "resolved_pct": round(resolved / internal * 100, 1) if internal else 0.0,
    }


def _safe_relpath(value: str) -> str | None:
    candidate = value.replace("\\", "/").strip()
    if not candidate or candidate.startswith("/") or ":" in candidate:
        return None
    normalized = posixpath.normpath(candidate)
    if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
        return None
    return normalized


def create_app(root: Path) -> FastAPI:
    root = Path(root).expanduser().resolve()
    app = FastAPI(
        title="Atlas",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def _open():
        try:
            conn = open_graph(root)
        except FileNotFoundError:
            raise HTTPException(status_code=409, detail=NO_GRAPH) from None
        if graph_schema(conn) != SCHEMA_VERSION:
            conn.close()
            raise HTTPException(status_code=409, detail=STALE_GRAPH)
        return conn

    def _model():
        try:
            info = detect_model(timeout=0.4)
        except Exception:
            return None
        return info

    @app.middleware("http")
    async def _headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'"
        )
        return response

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/api/meta")
    def meta():
        conn = _open()
        try:
            counts = _graph_counts(conn)
            tree = _file_index(root, conn)
        finally:
            conn.close()
        info = _model()
        return {
            "root": str(root),
            "bind_host": BIND_HOST,
            "schema": SCHEMA_VERSION,
            **counts,
            "tree": tree,
            "model": info.summary() if info else None,
            "no_model_message": NO_MODEL_MESSAGE,
            "refusal": REFUSAL,
        }

    @app.get("/api/file")
    def file_view(path: str = Query(min_length=1, max_length=512)):
        requested = _safe_relpath(path)
        if requested is None:
            raise HTTPException(status_code=404, detail="File is not indexed")
        conn = _open()
        try:
            index = _file_index(root, conn)
        finally:
            conn.close()
        entry = next((item for item in index if item["path"] == requested), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="File is not indexed")

        target = (root / requested).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(status_code=404, detail="File is not indexed")
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise HTTPException(status_code=404, detail="File could not be read") from None

        lines = text.splitlines()
        total = len(lines)
        truncated = total > MAX_SOURCE_LINES
        return {
            "path": requested,
            "language": entry["language"],
            "lines": lines[:MAX_SOURCE_LINES],
            "total_lines": total,
            "truncated": truncated,
        }

    @app.get("/api/map")
    def map_view(
        level: str = Query(default="file", pattern="^(file|package)$"),
        limit: int = Query(default=40, ge=1, le=200),
    ):
        conn = _open()
        conn.close()
        try:
            data = build_map(root, level, limit)
        except FileNotFoundError:
            raise HTTPException(status_code=409, detail=NO_GRAPH) from None
        payload = json.loads(to_json(data))
        payload["mermaid"] = to_mermaid(data)
        return payload

    @app.post("/api/ask")
    def ask(body: AskBody):
        conn = _open()
        try:
            retrieval = retrieve(
                conn,
                body.question,
                top_k=body.top_k,
                budget=body.budget,
                min_score=body.min_score,
            )
            file_lines = {
                row["path"]: row["lines"]
                for row in conn.execute("SELECT path, lines FROM files")
            }
        finally:
            conn.close()

        payload = retrieval.to_dict()
        if retrieval.refused:
            return {
                "mode": "refused",
                "answer": REFUSAL,
                "refused": True,
                "reason": retrieval.reason,
                "retrieval": payload,
            }

        info = _model()
        if info is None:
            return {
                "mode": "no-model",
                "message": NO_MODEL_MESSAGE,
                "refused": False,
                "retrieval": payload,
            }

        try:
            result = answer(body.question, retrieval, file_lines, model=info)
        except LLMUnavailable as exc:
            return {
                "mode": "error",
                "error": f"Local model failed: {exc}",
                "retrieval": payload,
            }

        response = result.to_dict()
        response["mode"] = "answer"
        return response

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app
