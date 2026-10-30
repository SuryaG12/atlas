# Atlas — Codebase Explainer

Working title. A local-first tool that reads a repository and explains it:
architecture map, "where is X implemented?", and plain-English diffs — every
claim cited to `path:line`.

## Why this exists

New contributors (and future-you, six months later) lose days to orientation.
Existing tools (Cody, Greptile, Bloop) are cloud/IDE/PR-centric and answer
questions but don't *teach the shape of the repo*. Atlas is:

1. **Local-first** — code never leaves the machine unless you opt in.
2. **Graph-grounded** — a deterministic symbol/import/call graph does the
   finding; the LLM only narrates. Fewer hallucinations, checkable answers.
3. **Onboarding-shaped** — outputs a map and a reading order, not just chat.
4. **Eval-first** — ships with a reproducible benchmark. Most projects can't
   say whether they're good.

Non-goals for v1: editing code, multi-repo search, runtime tracing,
hosted SaaS, supporting every language.

## CLI surface

```bash
atlas index .                      # build/refresh the index
atlas stats                        # languages, LOC, entry points, hotspots
atlas map --format mermaid         # module dependency diagram
atlas ask "where is auth handled?" # answer + citations
atlas explain src/api/routes.py    # file walkthrough
atlas tour                         # suggested reading order
atlas diff HEAD~1                  # what changed, in English
```

## Pipeline

```
ingest -> parse -> graph -> chunk -> embed -> retrieve -> answer -> render
                          \-> summarize (hierarchical, cached) -/
```

### 1. ingest
- gitignore-aware walker, skip binaries/vendored dirs, size caps.
- language detection, LOC stats, entry-point heuristics
  (`main`, `__main__`, `package.json` scripts, `cmd/`, `manage.py`, …).
- content hash per file -> incremental re-index.

### 2. parse
- tree-sitter queries per language (start: Python, TypeScript, Go).
- extract: symbols (function/class/method), signatures, docstrings, imports,
  call sites, inheritance, route decorators.
- unresolved calls are kept as name-only edges; resolution is a later pass.

### 3. graph (SQLite)
```sql
files(id, path, lang, loc, sha)
symbols(id, file_id, name, kind, start_line, end_line, signature, doc)
edges(id, src, dst, kind)      -- imports | calls | inherits | contains | routes
chunks(id, file_id, symbol_id, text, start_line, end_line, token_count)
embeddings(chunk_id, vector)   -- sqlite-vec or FAISS
summaries(id, scope, ref_id, text)
```

### 4. chunk + summarize
- chunk on AST boundaries (one symbol = one chunk); fall back to overlapping
  line windows for unparseable files.
- every chunk is prefixed with `path`, enclosing symbol, and signature so it
  is self-describing when retrieved.
- hierarchical summaries built bottom-up: symbol -> file -> package -> repo.
  Cached by content hash; only regenerate what changed.

### 5. retrieve (this is where toy projects fail)
- **Exact symbol pass** — identifiers in the question are looked up in the graph.
- **Lexical** — SQLite FTS5 over code + docstrings.
- **Semantic** — vector search over chunks.
- **Graph expansion** — pull callers/callees/importers of top hits (1 hop).
- Fuse with reciprocal rank fusion, rerank top 20, then pack to a token budget
  with sibling signatures for orientation.

### 6. answer
- Prompt contains: question, packed chunks, graph facts, repo summary.
- Every sentence that asserts something about code must carry `path:start-end`.
- **Post-validate citations** against the index; drop or flag anything that
  doesn't resolve. If retrieval confidence is low, say "not found" — no guessing.
- Output: markdown; Mermaid for flows.

### 7. render
- Typer CLI (rich tables, clickable paths).
- FastAPI + static frontend: file tree, graph view, answer pane with citations
  that jump to source.

## Retrieval quality plan

| Question type        | Primary strategy                  |
|----------------------|-----------------------------------|
| "where is X defined" | symbol lookup                     |
| "where is X used"    | reverse call/import edges         |
| "how does X work"    | semantic + graph expansion        |
| "what breaks if I change X" | forward callers, tests referencing X |
| "what did this PR do"| diff hunks -> changed symbols -> callers |

## Evaluation (the differentiator)

Build once, publish forever:

- 3 repos: small (~5k LOC), medium (~50k), large (~300k).
- 20 questions each, ground truth = file + symbol.
- Metrics: retrieval recall@5, citation validity %, answer faithfulness
  (rubric + LLM judge, spot-checked by hand), p50 latency, index time.
- `atlas eval` runs the suite; results table goes in the README.

Target: **recall@5 > 0.85**, **citation validity > 98%**, index a 50k-LOC repo
in under 60s.

## Milestones

| # | Deliverable | Acceptance |
|---|-------------|------------|
| M0 | Walker + stats + entry points | `atlas index .` reports in <5s on 10k LOC |
| M1 | tree-sitter symbols + graph + `map` | valid Mermaid module graph |
| M2 | Hybrid retrieval + `ask` with citations | 80% recall@5 on 20-question set |
| M3 | Web UI with clickable citations | answer -> source in one click |
| M4 | `diff` mode + incremental indexing | re-index only changed files |
| M5 | Eval harness + demo + writeup | results table + 2-min video |

Roughly one weekend each.

## Stack

- Python 3.11, Typer, Rich
- tree-sitter (+ grammars for py/ts/go)
- SQLite + FTS5 + sqlite-vec
- Embeddings: local (`sentence-transformers` / nomic) by default; API optional
- LLM: pluggable provider behind one interface (`answer`, `summarize`, `judge`)
- FastAPI + minimal frontend (no build step)

## Risks

| Risk | Mitigation |
|------|------------|
| Hallucinated citations | validate every citation against the index |
| Context window blowup | hierarchical summaries + budgeted packing |
| Polyglot repos | ship 3 languages well, fail loudly on the rest |
| Embedding cost/latency | local model, cache by content hash |
| "Just another chatbot" | the map, the tour, the diff mode, the eval table |

## Positioning

Not competing with Cody/Greptile on autocomplete or PR review. Atlas wins on
**local-first onboarding + teachable output + published evals**. Demo it on a
repo people know (FastAPI, Flask, or `requests`) so viewers can check the
answers themselves.

## Definition of done

A new contributor can answer "how do I add a new endpoint here?" using only
Atlas output, in under five minutes, on a repo they've never seen.
