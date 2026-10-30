# Atlas

Local-first codebase explainer. Point it at a repository, and it builds a graph
of files, symbols, and calls, then answers questions about the code with
citations to `path:line`.

The graph does the finding. The LLM only narrates what retrieval already found,
and every quote it produces is validated against the index before it is shown.
No embeddings, no cloud, no account. Code stays on your machine.

**Status: M0-M4 complete.** 86 tests passing. See [SPEC.md](SPEC.md) for the
design and [Roadmap](#roadmap) for what comes next.

## How it works

1. **Index** - walk the repository (gitignore-aware), parse symbols with Python
   `ast` and tree-sitter, and store files, symbols, edges, and import edges in
   `.atlas/graph.db`. Re-indexing is incremental by default: files whose SHA-256
   is unchanged are not parsed again.
2. **Retrieve** - a question is matched against the graph with three weak
   signals: exact/prefix symbol lookup, SQLite FTS5 (BM25) over symbol text, and
   one-hop graph expansion (callers, callees, subclasses, importers). Reciprocal
   rank fusion picks the top chunks.
3. **Narrate** - the retrieved chunks are the only context the model sees. It is
   asked to cite `path:line` / `path:start-end`.
4. **Validate** - every citation must point at a real path, a real line range,
   and overlap a retrieved chunk. Invalid ones are replaced with
   `[invalid citation removed]`; they are never quietly kept.

If retrieval scores below the threshold and no symbol matches exactly, Atlas
refuses with `I couldn't find that in this repo.` instead of guessing.

## Install

Python 3.14 is recommended: tree-sitter publishes wheels for it, which gives
JavaScript/TypeScript/Go parsing. Python parsing uses the stdlib `ast` and works
on any supported Python; on 3.15 the tree-sitter extras are skipped
automatically, so non-Python files are walked but not parsed.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

For development and tests:

```bash
pip install -e ".[dev]"
```

## Commands

```bash
atlas index .                     # incremental update of .atlas/graph.db
atlas index . --full              # force a complete rebuild
atlas diff .                      # working tree vs the last indexed state
atlas diff . --json               # same groups, machine-readable
atlas stats .                     # walker stats: languages, entry points, largest files
atlas map .                       # Mermaid import graph (falls back to file level)
atlas map . --level file --format json
atlas map . --format text          # readable edge list
atlas map . --out docs/graph.mmd   # write Mermaid to a file

atlas ask "where is citation validation done?"              # cited answer
atlas ask "where is citation validation done?" --no-llm     # retrieval only
atlas ask "where is citation validation done?" --json       # full JSON payload
atlas ask "how does indexing work?" --top-k 5 --budget 3000 # tune retrieval

atlas web                          # serve the UI and open a browser
atlas web --port 8000              # choose a port
atlas web --no-browser             # print the URL only
atlas web --path ../other-repo     # serve a different indexed repo
```

`atlas ask` uses a **local model only**. It looks for Ollama at
`http://127.0.0.1:11434` (first installed model, or `ATLAS_MODEL`), otherwise an
OpenAI-compatible server named by `ATLAS_LLM_URL`. With no model reachable it
says so plainly and exits 2 without guessing. `--no-llm` always works and prints
what retrieval found.

| Variable | Meaning |
|---|---|
| `ATLAS_LLM_URL` | OpenAI-compatible base URL or full `/chat/completions` |
| `ATLAS_LLM_MODEL` | model name for that server |
| `ATLAS_LLM_API_KEY` | optional bearer token |
| `ATLAS_OLLAMA_URL` | override the Ollama base URL |
| `ATLAS_MODEL` | Ollama model name (default: first installed) |
| `ATLAS_LLM_TIMEOUT` | request timeout in seconds (default 300) |

## Web UI

`atlas web` is the M3 surface: a single page, three panes, no build step and no
frontend framework.

- **Files** - the tree comes from the index. Clicking a file opens it with line
  numbers.
- **Ask** - the box calls the same `retrieve`, `answer`, and
  `validate_citations` functions as `atlas ask`, so the answer, the
  `[invalid citation removed]` handling, and the refusal are identical.
- **Modules** - the `atlas map` graph rendered as Mermaid, file level by default
  and package level on demand. Mermaid is vendored, so it renders offline.
- **Citations** - every `path:line` / `path:start-end` is clickable and opens
  that file, scrolls to the line, and highlights the range.
- **No model** - the header reads `no local model`, and the ask box still shows
  the retrieved chunks, exactly like `--no-llm`.

**Local-only.** The server binds to `127.0.0.1` only. There is no `0.0.0.0`
option, no CORS, no API docs, and a same-origin Content-Security-Policy. Assets
are served from the package; the page makes no external requests.

**Read-only.** The only routes are `GET /`, `GET /api/meta`,
`GET /api/file`, `GET /api/map`, and `POST /api/ask`, which runs retrieval in
memory. Nothing writes, edits, or deletes files, and file reads are limited to
paths present in the index (no absolute paths, no traversal).

Design is ported from the Dayo Venn system (not the Framer template): dark shell,
oversized tight `ATLAS` wordmark, dense 13-14px panes, hairline borders, one
accent (`#38BDF8`) for links, citations, and the active file, and a single
staggered entrance. Clash Grotesk / Satoshi / JetBrains Mono are used when
installed, falling back to Inter and system monospace. The native text cursor is
kept inside code and answers.

## What M0 does

- **Gitignore-aware walk** - root and nested `.gitignore`, negation rules, and
  default excludes (build output, `node_modules`, `vendor`, binaries, lockfiles).
- **Binary and size guards** - NUL-byte sniffing, 2 MB cap, symlinks never
  followed.
- **Language stats** - files/lines/share per language, code vs. prose split.
- **Entry points** - Python `__main__` guards, FastAPI/Flask/uvicorn, Django
  `manage.py`, `package.json` `bin`/`scripts`, Go/Rust/C/Java/C#/Kotlin `main`,
  shebang scripts. Test paths are excluded.
- **Manifest** - `.atlas/manifest.json` with language, lines, bytes, and
  SHA-256 per file.

## What M1 does

- **Python parser** (`ast`) - functions, methods, classes, decorators,
  signatures, imports (including relative), calls with receivers, inheritance.
- **tree-sitter parsers** - JavaScript, TypeScript/TSX, Go: symbols, methods,
  arrow-function bindings, imports, calls, `extends` / Go receiver types.
- **Graph** (`.atlas/graph.db`, SQLite) - files, symbols, edges, imports, with
  a parent link for nested symbols.
- **Call resolution** - same-file, `self`/`this` method dispatch, import-aware
  module resolution (including Python relative imports), then unique-name
  matching that requires the receiver to match the target's owner. Resolution
  is reported against *internal* edges only, because external calls to
  libraries can never resolve.
- **`atlas map`** - package or file level, Mermaid / text / JSON.

## What M2 does

No embeddings; three weak, complementary signals carry retrieval.

- **Symbol lookup** - identifier tokens from the question (camelCase and
  snake_case split, stopwords dropped) matched exactly, then by prefix or
  substring, against `symbols.name` and `symbols.qualified_name`.
- **FTS5** - BM25 over a `chunks` table that stores one row per symbol, indexed
  on name, path, code text, and docstring (Python docstrings, leading comments
  for tree-sitter languages).
- **One-hop graph expansion** - callers, callees, subclasses, base classes, and
  importers of the top seeds, so the answer can name the thing that *uses* the
  code you asked about.
- **Reciprocal rank fusion** - `1 / (60 + rank)` summed per source, so chunks
  found by more than one source win. Top ~8 symbol-shaped chunks (path, symbol,
  calls, then code) are packed into a token budget, with a small allowance for
  sibling signatures from the same file or parent.
- **Refusal** - below the fused-score threshold (`--min-score`, default 0.02)
  and with no exact symbol match, retrieval refuses and `atlas ask` prints
  exactly `I couldn't find that in this repo.` instead of guessing.
- **Citation validation** - every `path:line` or `path:start-end` in the model's
  answer is checked: the path must exist in the index, the range must be real
  and inside the file, and it must overlap a retrieved chunk. Failures are
  replaced inline with `[invalid citation removed]` and listed on stderr.

## What M3 does

M3 is a surface, not a second implementation.

- **Thin adapter** - `atlas.web.create_app(root)` imports and calls the existing
  `retrieve`, `answer`, `validate_citations`, `build_map`, `to_mermaid`,
  `open_graph`, and `detect_model`. No retrieval, graph, or citation logic is
  duplicated.
- **Local by construction** - `BIND_HOST = "127.0.0.1"` is a constant, redirects
  and CORS are not configured, API docs are disabled, and a
  Content-Security-Policy limits the page to same-origin assets.
- **Read-only** - only `GET` routes plus `POST /api/ask`; the ask route runs
  retrieval in memory. No endpoint writes, edits, or deletes files.
- **Index-checked** - the server validates the graph schema at startup, matching
  the CLI's behavior on a stale index.
- **Tests** - 20 `TestClient` tests cover the page, meta/tree parity with the
  index, model present/absent, file reads and traversal rejection, map levels,
  refusal, cited answers, invalid-citation stripping, and web-vs-CLI parity.

## What M4 does

M4 makes re-indexing cheap and makes the delta visible. It reuses the M0
manifest and the M1-M2 tables; retrieval, citation validation, parsing, and the
web UI are unchanged.

- **Incremental index** - `.atlas/manifest.json` already stores a SHA-256 per
  file. On the next `atlas index`, an unchanged file is skipped entirely: its
  symbols, chunks, edges, imports, and FTS5 rows stay in place. A changed or new
  file is re-parsed and only that file's rows are replaced. A deleted file has
  its rows removed.
- **Global resolution** - import targets and call edges are re-resolved against
  the whole graph, so a changed file can break or fix an edge in a file that did
  not change.
- **Safe fallback** - `--full` forces a rebuild. A missing, corrupt, or
  mismatched manifest/graph (schema, shas, or a SQLite error during the update)
  falls back to a complete rebuild with a plain warning. The incremental update
  runs in one transaction, so a failure cannot leave a half index.
- **Reporting** - every index reports files, parsed (changed + new), skipped,
  deleted, symbols, chunks, edges, and resolution. A second index on an
  unchanged repo parses 0 files.
- **`atlas diff`** - compares the working tree with the last indexed state:
  added/deleted/changed files (with old/new SHA), symbols added/removed/changed
  as `path:line`, and edge deltas summarised per file. `--json` emits the same
  groups. A clean repo prints `No changes.` rather than an error.
- **Read-only diff** - the graph is opened with `mode=ro` and the manifest is
  only read, so `atlas diff` never changes what the next index sees.
- **Web parity** - `atlas web` reads the manifest/graph directly, so it reflects
  an incremental index with no web changes and no second index implementation.
- **Tests** - 17 new tests cover unchanged second indexes, changed/added/deleted
  files, diff text/JSON parity, clean diffs, incremental-vs-`--full` snapshot
  equality, corrupt/missing fallback, and web parity after an incremental run.

## Numbers

Measured on this repository with `atlas index .` after the M4 files were added:

| Files | Lines | Symbols | Chunks | Edges | Internal resolved |
|---|---|---|---|---|---|
| 33 | 8,345 | 341 | 341 | 2,510 | 97.0% |

The index is local and disposable: `.atlas/` is gitignored. A full rebuild takes
well under a second on a repo this size, and a second index with no changes
parses 0 files (33 skipped) in about 0.01s. Non-Python languages need Python 3.14
(or another version with tree-sitter wheels); otherwise they are walked but not
parsed.

## Dev

```bash
pytest -q
```

The suite covers M0-M4: the walker and manifest, Python and tree-sitter parsing,
graph resolution, incremental indexing and fallbacks, diff text/JSON parity,
retrieval and refusal, citation validation, the CLI, and the web endpoints.

## Roadmap

| # | Milestone | Status |
|---|-----------|--------|
| M0 | Walker, stats, entry points, manifest | done |
| M1 | tree-sitter symbols, graph, `atlas map` | done |
| M2 | Hybrid retrieval + cited `atlas ask` | done |
| M3 | Local read-only web UI | done |
| M4 | Diff mode + incremental indexing | done |
| M5 | Eval harness + public benchmark | next |

## License

MIT - see [LICENSE](LICENSE).
