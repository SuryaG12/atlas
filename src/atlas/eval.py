"""M5 evaluation harness: retrieval recall, citation validity, faithfulness, refusal.

The harness is deliberately separate from the M2 retrieval implementation: it
imports and calls the exact same ``retrieve`` function, then scores the chunks it
returns.  Narration is optional and only runs when a local model is configured.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .answer import (
    CITATION_RE,
    REFUSAL,
    CitationReport,
    answer,
    validate_citations,
)
from .graph import SCHEMA_VERSION, graph_schema, open_graph
from .narrate import LLMUnavailable, ModelInfo
from .retrieval import RetrievalChunk, RetrievalResult, retrieve

TOP_K = 8
BUDGET = 6000
MIN_SCORE = 0.02
RECALL_N = 5
CLAIM_MIN_WORDS = 1

REQUIRED_FIELDS = (
    "id",
    "question",
    "repo",
    "answerable",
    "expected_files",
    "expected_symbols",
    "expected_citations",
)

_LIST_FIELDS = ("expected_files", "expected_symbols", "expected_citations")


class EvalError(RuntimeError):
    """A question file, repo, or ground-truth entry is unusable."""


@dataclass(slots=True)
class Question:
    id: str
    question: str
    repo: str
    answerable: bool
    expected_files: list[str] = field(default_factory=list)
    expected_symbols: list[str] = field(default_factory=list)
    expected_citations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "question": self.question,
            "repo": self.repo,
            "answerable": self.answerable,
            "expected_files": list(self.expected_files),
            "expected_symbols": list(self.expected_symbols),
            "expected_citations": list(self.expected_citations),
        }


@dataclass(slots=True)
class RepoSpec:
    key: str
    path: Path
    label: str
    url: str = ""
    tag: str = ""
    commit: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "path": self.label,
            "url": self.url,
            "tag": self.tag,
            "commit": self.commit,
        }


# -- question / repo loading ------------------------------------------------


def _expect_type(value: Any, expected: type, field_name: str, where: str) -> None:
    if expected is bool:
        if not isinstance(value, bool):
            raise EvalError(f"{where}: '{field_name}' must be true or false")
        return
    if not isinstance(value, expected):
        raise EvalError(
            f"{where}: '{field_name}' must be a {expected.__name__}"
        )


def _non_empty_string(value: Any, field_name: str, where: str) -> str:
    _expect_type(value, str, field_name, where)
    if not value.strip():
        raise EvalError(f"{where}: '{field_name}' must not be empty")
    return value.strip()


def load_questions(path: Path) -> list[Question]:
    """Load and structurally validate a question file.

    Ground truth is not checked here; call :func:`verify_questions` with an
    indexed repo connection for that.
    """
    path = Path(path)
    if not path.is_file():
        raise EvalError(f"Question file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalError(f"Question file is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise EvalError("Question file must contain a JSON array of questions")
    if not raw:
        raise EvalError("Question file contains no questions")

    questions: list[Question] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw, start=1):
        where = f"Question #{index}"
        if not isinstance(entry, dict):
            raise EvalError(f"{where}: expected a JSON object")
        missing = [name for name in REQUIRED_FIELDS if name not in entry]
        if missing:
            raise EvalError(f"{where}: missing field(s): {', '.join(missing)}")

        question_id = _non_empty_string(entry["id"], "id", where)
        where = f"Question {question_id}"
        if question_id in seen:
            raise EvalError(f"Duplicate question id: {question_id}")
        seen.add(question_id)

        text = _non_empty_string(entry["question"], "question", where)
        repo = _non_empty_string(entry["repo"], "repo", where)
        _expect_type(entry["answerable"], bool, "answerable", where)
        answerable = bool(entry["answerable"])

        lists: dict[str, list[str]] = {}
        for name in _LIST_FIELDS:
            value = entry[name]
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise EvalError(
                    f"{where}: '{name}' must be a list of non-empty strings"
                )
            lists[name] = [item.strip() for item in value]

        if answerable and not any(lists[name] for name in _LIST_FIELDS):
            raise EvalError(
                f"{where}: an answerable question needs at least one expected"
                " file, symbol, or citation"
            )
        if not answerable and any(lists[name] for name in _LIST_FIELDS):
            raise EvalError(
                f"{where}: an unanswerable question must not declare expected"
                " files, symbols, or citations"
            )
        if answerable:
            for citation in lists["expected_citations"]:
                try:
                    parse_citation(citation)
                except ValueError as exc:
                    raise EvalError(f"{where}: {exc}") from exc

        questions.append(
            Question(
                id=question_id,
                question=text,
                repo=repo,
                answerable=answerable,
                expected_files=lists["expected_files"],
                expected_symbols=lists["expected_symbols"],
                expected_citations=lists["expected_citations"],
            )
        )
    return questions


def load_repos(path: Path) -> dict[str, RepoSpec]:
    """Load a ``repos.json`` map. Relative paths resolve against the file."""
    path = Path(path)
    if not path.is_file():
        raise EvalError(f"Repo file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalError(f"Repo file is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not raw:
        raise EvalError("Repo file must be a non-empty JSON object")

    base = path.parent
    specs: dict[str, RepoSpec] = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise EvalError("Repo file contains an empty repo key")
        if not isinstance(entry, dict):
            raise EvalError(f"Repo '{key}': expected a JSON object")
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise EvalError(f"Repo '{key}': 'path' must be a non-empty string")
        repo_path = Path(raw_path).expanduser()
        if not repo_path.is_absolute():
            repo_path = base / repo_path
        repo_path = repo_path.resolve()
        if not repo_path.is_dir():
            raise EvalError(f"Repo '{key}': path does not exist: {raw_path}")
        specs[key] = RepoSpec(
            key=key,
            path=repo_path,
            label=raw_path,
            url=str(entry.get("url") or ""),
            tag=str(entry.get("tag") or ""),
            commit=str(entry.get("commit") or ""),
        )
    return specs


def resolve_repos(
    questions: list[Question], repos_path: Path | None = None
) -> dict[str, RepoSpec]:
    """Resolve every question's repo key to a directory.

    A missing ``repos.json`` entry is allowed when the key itself is a directory
    path, which keeps synthetic tests and ad-hoc repos simple.
    """
    specs = load_repos(repos_path) if repos_path is not None else {}
    resolved: dict[str, RepoSpec] = {}
    for question in questions:
        if question.repo in resolved:
            continue
        if question.repo in specs:
            resolved[question.repo] = specs[question.repo]
            continue
        candidate = Path(question.repo).expanduser()
        if candidate.is_dir():
            resolved[question.repo] = RepoSpec(
                key=question.repo,
                path=candidate.resolve(),
                label=question.repo,
            )
            continue
        raise EvalError(
            f"Repo '{question.repo}' is not defined in the repo file and is not"
            " a directory. Add it to repos.json (or pass --repos)."
        )
    return resolved


# -- ground truth verification ---------------------------------------------


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def parse_citation(value: str) -> tuple[str, int, int]:
    """Parse ``path:line`` or ``path:start-end`` into a normalized tuple."""
    path, separator, rng = value.rpartition(":")
    if not separator or not path or not rng:
        raise ValueError(
            f"bad citation '{value}': expected path:line or path:start-end"
        )
    if "-" in rng:
        start_text, _, end_text = rng.partition("-")
    else:
        start_text = end_text = rng
    if not start_text.isdigit() or not end_text.isdigit():
        raise ValueError(f"bad citation '{value}': line numbers must be integers")
    start, end = int(start_text), int(end_text)
    if start < 1 or end < start:
        raise ValueError(f"bad citation '{value}': invalid line range")
    return _normalize_path(path), start, end


def _citation_label(path: str, start: int, end: int, has_range: bool) -> str:
    return f"{path}:{start}" + (f"-{end}" if has_range else "")


def symbol_matches(expected: str, chunk_name: str) -> bool:
    """Match a plain or qualified expected symbol against a chunk's name.

    Chunk names are qualified names (``Class.method``).  A plain expected name
    also matches the final component so answers can cite methods by name.
    """
    if not expected or not chunk_name:
        return False
    if "." in expected:
        return chunk_name == expected or chunk_name.endswith("." + expected)
    return chunk_name == expected or chunk_name.rsplit(".", 1)[-1] == expected


def _symbol_exists(conn: sqlite3.Connection, expected: str) -> bool:
    if "." in expected:
        row = conn.execute(
            "SELECT 1 FROM symbols WHERE qualified_name = ?"
            " OR qualified_name LIKE ? LIMIT 1",
            (expected, "%." + expected),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM symbols WHERE name = ? LIMIT 1", (expected,)
        ).fetchone()
    return row is not None


def _citation_problem(
    conn: sqlite3.Connection, files: dict[str, int], citation: str
) -> str | None:
    try:
        path, start, end = parse_citation(citation)
    except ValueError as exc:
        return str(exc)
    if path not in files:
        return f"citation '{citation}' points at a file that is not indexed"
    if end > files[path]:
        return (
            f"citation '{citation}' exceeds the file length"
            f" ({files[path]} lines)"
        )
    hit = conn.execute(
        "SELECT 1 FROM chunks WHERE path=? AND start_line<=? AND ?<=end_line LIMIT 1",
        (path, end, start),
    ).fetchone()
    if hit is None:
        return f"citation '{citation}' is not covered by any indexed chunk"
    return None


def verify_question(
    conn: sqlite3.Connection, question: Question, files: dict[str, int] | None = None
) -> list[str]:
    """Return every ground-truth problem for one answerable question."""
    if not question.answerable:
        return []
    if files is None:
        files = {
            row["path"]: row["lines"]
            for row in conn.execute("SELECT path, lines FROM files")
        }

    problems: list[str] = []
    for path in question.expected_files:
        normalized = _normalize_path(path)
        if normalized not in files:
            problems.append(
                f"expected file '{path}' is not in the index"
            )
    for symbol in question.expected_symbols:
        if not _symbol_exists(conn, symbol):
            problems.append(
                f"expected symbol '{symbol}' is not in the index"
            )
    for citation in question.expected_citations:
        problem = _citation_problem(conn, files, citation)
        if problem:
            problems.append(problem)
    return problems


def verify_questions(
    conn: sqlite3.Connection, questions: list[Question]
) -> None:
    """Reject the whole run if any ground-truth entry cannot be verified."""
    files = {
        row["path"]: row["lines"]
        for row in conn.execute("SELECT path, lines FROM files")
    }
    errors: list[str] = []
    for question in questions:
        for problem in verify_question(conn, question, files):
            errors.append(f"- {question.id}: {problem}")
    if errors:
        raise EvalError(
            "Ground truth verification failed; refusing to score unverifiable"
            " questions:\n" + "\n".join(errors)
        )


# -- scoring ----------------------------------------------------------------


def _chunk_matches(
    chunk: RetrievalChunk,
    expected_files: list[str],
    expected_symbols: list[str],
) -> tuple[bool, bool]:
    files = {_normalize_path(path) for path in expected_files}
    file_hit = _normalize_path(chunk.path) in files
    symbol_hit = any(
        symbol_matches(symbol, chunk.name) for symbol in expected_symbols
    )
    return file_hit, symbol_hit


def score_retrieval(
    question: Question,
    result: RetrievalResult,
    *,
    top_n: int = RECALL_N,
) -> dict:
    """Score one retrieval result against one question's ground truth."""
    top = result.chunks[:top_n]
    file_hit = False
    symbol_hit = False
    for chunk in top:
        chunk_file_hit, chunk_symbol_hit = _chunk_matches(
            chunk, question.expected_files, question.expected_symbols
        )
        file_hit = file_hit or chunk_file_hit
        symbol_hit = symbol_hit or chunk_symbol_hit
    return {
        "file_hit": file_hit,
        "symbol_hit": symbol_hit,
        "recall_at_5": file_hit or symbol_hit,
        "top": [chunk.summary() for chunk in top],
    }


def citation_validity(reports: list[CitationReport]) -> float | None:
    """Valid citations / total citations, or None if there were none."""
    valid = sum(len(report.valid) for report in reports)
    invalid = sum(len(report.invalid) for report in reports)
    total = valid + invalid
    return round(valid / total, 6) if total else None


def split_claims(text: str) -> list[str]:
    """Split narration into sentence-level claims, skipping headings/citations."""
    claims: list[str] = []
    for block in re.split(r"\n+", text):
        line = block.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)
        for piece in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9`_*])", line):
            claim = piece.strip()
            if not claim:
                continue
            if CITATION_RE.fullmatch(claim):
                continue
            if not re.search(r"[A-Za-z]{2,}", claim):
                continue
            words = re.findall(r"[A-Za-z0-9_]+", claim)
            if len(words) < CLAIM_MIN_WORDS:
                continue
            if (
                claim.endswith(":")
                and len(words) <= 6
                and not CITATION_RE.search(claim)
            ):
                continue
            if claim.lower().rstrip(":") in {
                "source",
                "sources",
                "citation",
                "citations",
                "references",
            }:
                continue
            claims.append(claim)
    return claims


def _contained(
    chunks: list[RetrievalChunk], path: str, start: int, end: int
) -> bool:
    return any(
        _normalize_path(chunk.path) == path
        and chunk.start <= start
        and end <= chunk.end
        for chunk in chunks
    )


def score_faithfulness(
    text: str,
    chunks: list[RetrievalChunk],
    file_lines: dict[str, int],
) -> dict:
    """Score claims by citation containment.

    A claim passes only when it contains a citation that ``validate_citations``
    accepts *and* whose span is fully contained in one of the retrieved chunks.
    An uncited claim fails.
    """
    _, report = validate_citations(text, chunks, file_lines)
    valid = set(report.valid)
    claims = split_claims(text)
    faithful = 0
    uncited: list[str] = []
    unsupported: list[str] = []
    for claim in claims:
        passed = False
        found = False
        for match in CITATION_RE.finditer(claim):
            found = True
            has_range = match.group(3) is not None
            label = _citation_label(
                _normalize_path(match.group(1)),
                int(match.group(2)),
                int(match.group(3) or match.group(2)),
                has_range,
            )
            path, start, end = parse_citation(label)
            if label in valid and _contained(chunks, path, start, end):
                passed = True
                break
        if passed:
            faithful += 1
        elif found:
            unsupported.append(claim)
        else:
            uncited.append(claim)
    total = len(claims)
    return {
        "claims": total,
        "faithful_claims": faithful,
        "faithfulness": round(faithful / total, 6) if total else None,
        "uncited_claims": uncited,
        "unsupported_claims": unsupported,
    }


def _exact_name_question(question: Question) -> bool:
    lowered = question.question.lower()
    for symbol in question.expected_symbols:
        short = symbol.rsplit(".", 1)[-1]
        if short.lower() in lowered:
            return True
        if symbol.lower() in lowered:
            return True
    return False


def _aggregate(outcomes: list["QuestionOutcome"]) -> dict:
    answerable = [outcome for outcome in outcomes if outcome.answerable]
    unanswerable = [outcome for outcome in outcomes if not outcome.answerable]
    recall_hits = sum(1 for outcome in answerable if outcome.recall_at_5)
    refusals_correct = sum(1 for outcome in unanswerable if outcome.refused)
    retrieval_refusals = sum(1 for outcome in answerable if outcome.refused)

    narration = [
        outcome.narration
        for outcome in outcomes
        if outcome.narration and outcome.narration["status"] == "ok"
    ]
    valid = sum(len(record["valid_citations"]) for record in narration)
    invalid = sum(len(record["invalid_citations"]) for record in narration)
    citation_total = valid + invalid
    claims = sum(record["claims"] for record in narration)
    faithful = sum(record["faithful_claims"] for record in narration)

    return {
        "total": len(outcomes),
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "recall_at_5": (
            round(recall_hits / len(answerable), 6) if answerable else None
        ),
        "recall_hits": recall_hits,
        "citation_validity": (
            round(valid / citation_total, 6) if citation_total else None
        ),
        "valid_citations": valid,
        "invalid_citations": invalid,
        "faithfulness": round(faithful / claims, 6) if claims else None,
        "faithful_claims": faithful,
        "claims": claims,
        "refusal_accuracy": (
            round(refusals_correct / len(unanswerable), 6)
            if unanswerable
            else None
        ),
        "refusals_correct": refusals_correct,
        "answerable_retrieval_refusals": retrieval_refusals,
        "narrated_questions": len(narration),
    }


@dataclass
class QuestionOutcome:
    id: str
    question: str
    repo: str
    answerable: bool
    refused: bool
    best_score: float
    refusal_reason: str
    exact_symbol_match: bool
    recall_at_5: bool | None
    file_hit: bool | None
    symbol_hit: bool | None
    top: list[dict]
    exact_name: bool
    expected_files: list[str]
    expected_symbols: list[str]
    expected_citations: list[str]
    narration: dict | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "question": self.question,
            "repo": self.repo,
            "answerable": self.answerable,
            "refused": self.refused,
            "best_score": round(self.best_score, 6),
            "refusal_reason": self.refusal_reason,
            "exact_symbol_match": self.exact_symbol_match,
            "recall_at_5": self.recall_at_5,
            "file_hit": self.file_hit,
            "symbol_hit": self.symbol_hit,
            "exact_name": self.exact_name,
            "expected_files": list(self.expected_files),
            "expected_symbols": list(self.expected_symbols),
            "expected_citations": list(self.expected_citations),
            "top": self.top,
            "narration": self.narration,
        }


@dataclass
class EvalReport:
    repos: dict[str, dict]
    outcomes: list[QuestionOutcome]
    metrics: dict
    paraphrase: dict
    narration: dict
    verified_questions: int

    def to_dict(self) -> dict:
        return {
            "repos": self.repos,
            "questions": [outcome.to_dict() for outcome in self.outcomes],
            "metrics": self.metrics,
            "paraphrase": self.paraphrase,
            "narration": self.narration,
            "verified_questions": self.verified_questions,
        }


# -- the run ----------------------------------------------------------------


def _repo_counts(conn: sqlite3.Connection) -> dict:
    return {
        "files": conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
        "symbols": conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
        "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "edges": conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
    }


def _narrate_question(
    question: Question,
    result: RetrievalResult,
    file_lines: dict[str, int],
    model: ModelInfo,
) -> dict:
    try:
        result_answer = answer(
            question.question, result, file_lines, model=model
        )
    except (LLMUnavailable, RuntimeError) as exc:
        return {
            "status": "error",
            "error": str(exc),
            "valid_citations": [],
            "invalid_citations": [],
            "claims": 0,
            "faithful_claims": 0,
            "faithfulness": None,
            "citation_validity": None,
        }

    if result_answer.refused:
        return {
            "status": "refused",
            "answer": REFUSAL,
            "valid_citations": [],
            "invalid_citations": [],
            "claims": 0,
            "faithful_claims": 0,
            "faithfulness": None,
            "citation_validity": None,
        }

    faith = score_faithfulness(result_answer.answer, result.chunks, file_lines)
    valid = list(result_answer.citations.valid)
    invalid = [issue.summary() for issue in result_answer.citations.invalid]
    total = len(valid) + len(invalid)
    return {
        "status": "ok",
        "answer": result_answer.answer,
        "refused": False,
        "valid_citations": valid,
        "invalid_citations": invalid,
        "citation_validity": round(len(valid) / total, 6) if total else None,
        "claims": faith["claims"],
        "faithful_claims": faith["faithful_claims"],
        "faithfulness": faith["faithfulness"],
        "uncited_claims": faith["uncited_claims"],
        "unsupported_claims": faith["unsupported_claims"],
    }


def _paraphrase_diagnostic(outcomes: list[QuestionOutcome]) -> dict:
    answerable = [outcome for outcome in outcomes if outcome.answerable]
    groups = {
        "exact_name": [outcome for outcome in answerable if outcome.exact_name],
        "paraphrase": [
            outcome for outcome in answerable if not outcome.exact_name
        ],
    }
    payload: dict[str, Any] = {}
    for name, members in groups.items():
        hits = sum(1 for outcome in members if outcome.recall_at_5)
        payload[name] = {
            "questions": len(members),
            "recall_hits": hits,
            "recall_at_5": round(hits / len(members), 6) if members else None,
        }
    exact = payload["exact_name"]["recall_at_5"]
    paraphrase = payload["paraphrase"]["recall_at_5"]
    gap = None
    if exact is not None and paraphrase is not None:
        gap = round(exact - paraphrase, 6)
    payload["gap"] = gap
    payload["material"] = bool(gap is not None and gap >= 0.10)
    return payload


def run_eval(
    questions: list[Question],
    repos: dict[str, RepoSpec],
    *,
    model: ModelInfo | None = None,
    top_k: int = TOP_K,
    budget: int = BUDGET,
    min_score: float = MIN_SCORE,
) -> EvalReport:
    """Run retrieval for every question, then optional narration.

    Ground truth is verified per repo before any question is scored.  If one
    entry cannot be verified, the run fails rather than reporting a fake score.
    """
    if not questions:
        raise EvalError("No questions to evaluate")

    grouped: OrderedDict[str, list[Question]] = OrderedDict()
    for question in questions:
        grouped.setdefault(question.repo, []).append(question)

    repo_payload: dict[str, dict] = {}
    outcomes: list[QuestionOutcome] = []
    narration_errors: list[str] = []
    answerables_without_narration: dict[str, list[Question]] = {}

    for repo_key, repo_questions in grouped.items():
        spec = repos.get(repo_key)
        if spec is None:
            raise EvalError(f"Repo '{repo_key}' has no resolved path")
        if not spec.path.is_dir():
            raise EvalError(f"Repo '{repo_key}' path does not exist: {spec.label}")

        try:
            conn = open_graph(spec.path)
        except FileNotFoundError as exc:
            raise EvalError(
                f"Repo '{repo_key}' has no graph index at {spec.label}. Run"
                f" `atlas index {spec.label}` first."
            ) from exc

        try:
            if graph_schema(conn) != SCHEMA_VERSION:
                raise EvalError(
                    f"Repo '{repo_key}' has an out-of-date index (expected"
                    f" schema {SCHEMA_VERSION}). Re-run `atlas index"
                    f" {spec.label}`."
                )
            verify_questions(conn, repo_questions)
            file_lines = {
                row["path"]: row["lines"]
                for row in conn.execute("SELECT path, lines FROM files")
            }
            # Root-relative paths keep the report portable.
            counts = _repo_counts(conn)

            for question in repo_questions:
                result = retrieve(
                    conn,
                    question.question,
                    top_k=top_k,
                    budget=budget,
                    min_score=min_score,
                )
                score = score_retrieval(question, result)
                narration = None
                if question.answerable and model is not None:
                    if result.refused or not result.chunks:
                        narration = {
                            "status": "skipped",
                            "reason": "retrieval refused; nothing to narrate",
                            "valid_citations": [],
                            "invalid_citations": [],
                            "claims": 0,
                            "faithful_claims": 0,
                            "faithfulness": None,
                            "citation_validity": None,
                        }
                    else:
                        narration = _narrate_question(
                            question, result, file_lines, model
                        )
                        if narration["status"] == "error":
                            narration_errors.append(
                                f"{question.id}: {narration['error']}"
                            )
                elif question.answerable:
                    answerables_without_narration.setdefault(
                        repo_key, []
                    ).append(question)

                outcomes.append(
                    QuestionOutcome(
                        id=question.id,
                        question=question.question,
                        repo=question.repo,
                        answerable=question.answerable,
                        refused=result.refused,
                        best_score=result.best_score,
                        refusal_reason=result.reason,
                        exact_symbol_match=result.exact_symbol_match,
                        recall_at_5=(
                            score["recall_at_5"] if question.answerable else None
                        ),
                        file_hit=(
                            score["file_hit"] if question.answerable else None
                        ),
                        symbol_hit=(
                            score["symbol_hit"] if question.answerable else None
                        ),
                        top=score["top"],
                        exact_name=(
                            _exact_name_question(question)
                            if question.answerable
                            else False
                        ),
                        expected_files=list(question.expected_files),
                        expected_symbols=list(question.expected_symbols),
                        expected_citations=list(question.expected_citations),
                        narration=narration,
                    )
                )
        finally:
            conn.close()

        repo_outcomes = [
            outcome for outcome in outcomes if outcome.repo == repo_key
        ]
        repo_payload[repo_key] = {
            **spec.to_dict(),
            **counts,
            "metrics": _aggregate(repo_outcomes),
        }

    state = "not evaluated"
    reason = "no local model configured"
    model_payload = None
    if model is not None:
        model_payload = model.summary()
        state = "evaluated"
        reason = "local model narration was run"
        if narration_errors:
            state = "partial"
            reason = (
                f"{len(narration_errors)} narration request(s) failed;"
                " retrieval metrics are unaffected"
            )

    per_repo = {
        key: payload["metrics"] for key, payload in repo_payload.items()
    }
    overall = _aggregate(outcomes)
    metrics = {"per_repo": per_repo, "overall": overall}
    paraphrase = _paraphrase_diagnostic(outcomes)

    narration = {
        "status": state,
        "reason": reason,
        "model": model_payload,
        "citation_validity": overall["citation_validity"],
        "faithfulness": overall["faithfulness"],
        "claims": overall["claims"],
        "faithful_claims": overall["faithful_claims"],
        "valid_citations": overall["valid_citations"],
        "invalid_citations": overall["invalid_citations"],
        "errors": narration_errors,
        "skipped_answerable_without_narration": {
            key: [question.id for question in items]
            for key, items in answerables_without_narration.items()
        },
    }

    return EvalReport(
        repos=repo_payload,
        outcomes=outcomes,
        metrics=metrics,
        paraphrase=paraphrase,
        narration=narration,
        verified_questions=sum(
            1 for outcome in outcomes if outcome.answerable
        ),
    )


# -- rendering --------------------------------------------------------------


def _fmt(value: float | None, numerator: int | None = None, denominator: int | None = None) -> str:
    if value is None:
        return "not evaluated"
    text = f"{value:.3f}"
    if numerator is not None and denominator is not None:
        text += f" ({numerator}/{denominator})"
    return text


def _fmt_narration(
    value: float | None,
    numerator: int,
    denominator: int,
    status: str,
    empty: str,
) -> str:
    if status == "not evaluated":
        return "not evaluated"
    if value is None:
        return empty
    return _fmt(value, numerator, denominator)


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _top_line(outcome: QuestionOutcome) -> str:
    if not outcome.top:
        return "no retrieved chunks"
    parts = [
        f"`{chunk['path']}:{chunk['start']}-{chunk['end']}` {chunk['name']}"
        for chunk in outcome.top[:5]
    ]
    return "; ".join(parts)


def render_markdown(report: EvalReport) -> str:
    lines: list[str] = []
    overall = report.metrics["overall"]
    lines.append("# Atlas M5 evaluation")
    lines.append("")
    lines.append(
        "Retrieval numbers come from the exact M2 path:"
        " `retrieve(question, top_k=8, budget=6000, min_score=0.02)` with RRF"
        " fusion and top-8 packing. No embeddings were used."
    )
    lines.append("")
    narration = report.narration
    if narration["status"] == "not evaluated":
        lines.append(
            "**Narration: not evaluated** — no local model was configured."
            " Citation validity and faithfulness need narrated answers and are"
            " therefore reported as *not evaluated*, not as retrieval scores."
        )
    elif narration["status"] == "evaluated":
        model = narration["model"] or {}
        lines.append(
            f"**Narration: evaluated** with `{model.get('provider')}/"
            f"{model.get('model')}` at `{model.get('endpoint')}`."
        )
    else:
        lines.append(
            f"**Narration: partial** — {narration['reason']}."
        )
    lines.append("")

    lines.append("## Headline")
    lines.append("")
    lines.append("| Metric | Result |")
    lines.append("| --- | ---: |")
    lines.append(
        f"| Retrieval recall@5 |"
        f" {_fmt(overall['recall_at_5'], overall['recall_hits'], overall['answerable'])} |"
    )
    lines.append(
        f"| Refusal accuracy (M2 rule) |"
        f" {_fmt(overall['refusal_accuracy'], overall['refusals_correct'], overall['unanswerable'])} |"
    )
    lines.append(
        f"| Citation validity |"
        f" {_fmt_narration(overall['citation_validity'], overall['valid_citations'], overall['valid_citations'] + overall['invalid_citations'], narration['status'], 'n/a (no citations)')} |"
    )
    lines.append(
        f"| Faithfulness |"
        f" {_fmt_narration(overall['faithfulness'], overall['faithful_claims'], overall['claims'], narration['status'], 'n/a (no claims)')} |"
    )
    lines.append("")
    if (
        overall["refusal_accuracy"] is not None
        and overall["refusal_accuracy"] < 1.0
    ):
        lines.append(
            "The refusal number is weak: the M2 threshold accepts any question"
            " whose best fused score reaches 0.02, even when the matched code is"
            " unrelated. The refusal cases below name every false positive."
        )
    else:
        lines.append(
            "Every unanswerable question was refused under the M2 rule."
        )
    lines.append("")

    lines.append("## Repositories")
    lines.append("")
    lines.append(
        "| Repo | Pinned version | Commit | Files | Symbols | Chunks | Edges |"
    )
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: |")
    for key, repo in report.repos.items():
        version = repo.get("tag") or "unversioned"
        if repo.get("url"):
            version = f"[{version}]({repo['url']})"
        commit = repo.get("commit") or "not recorded"
        lines.append(
            f"| `{key}` | {version} | `{commit}` | {repo['files']} |"
            f" {repo['symbols']} | {repo['chunks']} | {repo['edges']} |"
        )
    lines.append("")

    lines.append("## Question set")
    lines.append("")
    lines.append("| Repo | Questions | Answerable | Unanswerable |")
    lines.append("| --- | ---: | ---: | ---: |")
    for key, metrics in report.metrics["per_repo"].items():
        lines.append(
            f"| `{key}` | {metrics['total']} | {metrics['answerable']} |"
            f" {metrics['unanswerable']} |"
        )
    lines.append(
        f"| **overall** | **{overall['total']}** | **{overall['answerable']}** |"
        f" **{overall['unanswerable']}** |"
    )
    lines.append("")

    lines.append("## Metrics")
    lines.append("")
    lines.append(
        "Recall@5 is the fraction of answerable questions with an expected file"
        " or symbol in the top 5 retrieved chunks. Citation validity and"
        " faithfulness are narration-only. Refusal accuracy uses the M2 rule:"
        " `best_score < 0.02` and no exact symbol match."
    )
    lines.append("")
    lines.append(
        "| Scope | Recall@5 | Citation validity | Faithfulness | Refusal accuracy |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for key, metrics in report.metrics["per_repo"].items():
        lines.append(
            f"| `{key}` | {_fmt(metrics['recall_at_5'], metrics['recall_hits'], metrics['answerable'])}"
            f" | {_fmt_narration(metrics['citation_validity'], metrics['valid_citations'], metrics['valid_citations'] + metrics['invalid_citations'], narration['status'], 'n/a (no citations)')}"
            f" | {_fmt_narration(metrics['faithfulness'], metrics['faithful_claims'], metrics['claims'], narration['status'], 'n/a (no claims)')}"
            f" | {_fmt(metrics['refusal_accuracy'], metrics['refusals_correct'], metrics['unanswerable'])} |"
        )
    lines.append(
        f"| **overall** | {_fmt(overall['recall_at_5'], overall['recall_hits'], overall['answerable'])}"
        f" | {_fmt_narration(overall['citation_validity'], overall['valid_citations'], overall['valid_citations'] + overall['invalid_citations'], narration['status'], 'n/a (no citations)')}"
        f" | {_fmt_narration(overall['faithfulness'], overall['faithful_claims'], overall['claims'], narration['status'], 'n/a (no claims)')}"
        f" | {_fmt(overall['refusal_accuracy'], overall['refusals_correct'], overall['unanswerable'])} |"
    )
    lines.append("")

    lines.append("## Retrieval failures")
    lines.append("")
    failures = [
        outcome
        for outcome in report.outcomes
        if outcome.answerable and not outcome.recall_at_5
    ]
    if not failures:
        lines.append("None. Every answerable question had an expected file or symbol in the top 5.")
    else:
        lines.append(
            f"{len(failures)} of {overall['answerable']} answerable questions"
            " missed the top 5. These are the real failures, not hidden:"
        )
        lines.append("")
        for outcome in failures:
            reason = (
                "retrieval refused"
                if outcome.refused
                else "no expected file or symbol in the top 5"
            )
            expected = []
            if outcome.expected_files:
                expected.append("files " + ", ".join(f"`{p}`" for p in outcome.expected_files))
            if outcome.expected_symbols:
                expected.append("symbols " + ", ".join(f"`{s}`" for s in outcome.expected_symbols))
            lines.append(
                f"- `{outcome.id}` ({outcome.repo}) — {_short(outcome.question)}"
                f" — **{reason}**. Expected {'; '.join(expected)}."
            )
            lines.append(f"  - Top 5: {_top_line(outcome)}")
            if outcome.refused:
                lines.append(f"  - Refusal reason: {outcome.refusal_reason}")
        lines.append("")

    lines.append("## Refusal cases")
    lines.append("")
    unanswerable = [
        outcome for outcome in report.outcomes if not outcome.answerable
    ]
    for outcome in unanswerable:
        verdict = "correct refusal" if outcome.refused else "**false positive**"
        if outcome.refused:
            detail = outcome.refusal_reason or "no symbol or text match"
        else:
            reasons = []
            if outcome.best_score >= MIN_SCORE:
                reasons.append(
                    f"best score {outcome.best_score:.4f} >= {MIN_SCORE:.2f}"
                )
            if outcome.exact_symbol_match:
                reasons.append("exact symbol match present")
            detail = "did not refuse"
            if reasons:
                detail += " (" + "; ".join(reasons) + ")"
        lines.append(
            f"- `{outcome.id}` ({outcome.repo}) — {verdict} —"
            f" {_short(outcome.question)}"
        )
        lines.append(
            f"  - best score {outcome.best_score:.4f}; exact symbol match:"
            f" {outcome.exact_symbol_match}; rule: {detail}"
        )
        if not outcome.refused:
            lines.append(f"  - Top 5: {_top_line(outcome)}")
    lines.append("")

    lines.append("## Paraphrase diagnostic")
    lines.append("")
    diagnostic = report.paraphrase
    exact = diagnostic["exact_name"]
    para = diagnostic["paraphrase"]
    lines.append(
        "Questions that contain the expected symbol name are compared with"
        " questions that describe the same code in different words."
    )
    lines.append("")
    lines.append("| Group | Questions | Recall@5 |")
    lines.append("| --- | ---: | ---: |")
    lines.append(
        f"| Exact code name in question | {exact['questions']} |"
        f" {_fmt(exact['recall_at_5'], exact['recall_hits'], exact['questions'])} |"
    )
    lines.append(
        f"| Paraphrase | {para['questions']} |"
        f" {_fmt(para['recall_at_5'], para['recall_hits'], para['questions'])} |"
    )
    lines.append("")
    if diagnostic["gap"] is None:
        lines.append(
            "A gap cannot be computed because one group has no questions."
        )
    else:
        lines.append(
            f"Gap (exact - paraphrase): **{diagnostic['gap']:+.3f}**."
            f" This is {'a material' if diagnostic['material'] else 'not a material'}"
            " gap at the 0.10 threshold. No embeddings were added in M5."
        )
    if exact["questions"] < 10 or para["questions"] < 10:
        lines.append("")
        lines.append(
            "Sample sizes are small (especially the exact-name group), so treat"
            " the gap as directional rather than a precise estimate."
        )
    lines.append("")

    lines.append("## Ground truth")
    lines.append("")
    lines.append(
        f"{report.verified_questions} answerable questions were verified against"
        " the indexed files, symbols, and chunk ranges before scoring."
        " Unverifiable questions would have aborted the run."
    )
    lines.append("")

    if narration["status"] != "not evaluated":
        lines.append("## Narration failures")
        lines.append("")
        if narration["errors"]:
            for error in narration["errors"]:
                lines.append(f"- {error}")
        else:
            lines.append("No narration request failed.")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_json(report: EvalReport) -> str:
    return json.dumps(report.to_dict(), indent=2) + "\n"
