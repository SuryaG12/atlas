"""Answer assembly and citation validation."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .narrate import ModelInfo, complete
from .retrieval import RetrievalChunk, RetrievalResult

REFUSAL = "I couldn't find that in this repo."

SYSTEM_PROMPT = """You are Atlas, a code explanation assistant.

Rules:
1. Use ONLY the code excerpts provided. Do not use outside knowledge about the repo.
2. Every factual claim about the code must carry a citation of the form `path:line`
   or `path:start-end`, using paths exactly as given in the excerpt headers.
3. Never invent paths, line numbers, or symbols.
4. If the excerpts do not answer the question, reply exactly:
   I couldn't find that in this repo.
5. Prefer the smallest range that contains the evidence. Be concise, use markdown,
   and mention the symbols you relied on by name.
"""

# A scoped ask receives every chunk from one file in reading order. The generic
# prompt's "be concise" is not enough there: the model tends to walk each symbol
# in turn until it hits the token cap. Ask for a short overview instead, with a
# flat bullet list and an explicit stopping point, so generation ends on its own.
SCOPED_GUIDANCE = """
This is a file overview request: every excerpt comes from one file, in reading order.
Write a short overview, not a symbol-by-symbol walkthrough:
- One or two sentences on the file's purpose.
- Then 3 to 5 bullets, each naming one important symbol and what it does.
- End every bullet with a citation using the real path from the excerpt header,
  for example `app/service.py:3-5`. Never write the word "path" as a placeholder.
- Do not describe every symbol or restate code line by line.
- Stop after the bullets. Aim for 150 words, never exceed 220.
"""

SCOPED_SYSTEM_PROMPT = SYSTEM_PROMPT + SCOPED_GUIDANCE

CITATION_RE = re.compile(
    r"`?\b([A-Za-z0-9_][A-Za-z0-9_./\\-]*\.[A-Za-z0-9_]+):(\d+)(?:-(\d+))?`?"
)

_PATH = r"[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.[A-Za-z0-9_]+"
CITATION_VARIANT_RE = re.compile(
    r"(?P<open>`)?(?P<path>" + _PATH + r")(?P<close>`)?\s+"
    r"(?:(?:at|on)\s+)?lines?\s+(?P<start>\d+)"
    r"(?:\s*(?:-|to)\s*(?P<end>\d+))?",
    re.IGNORECASE,
)


def normalize_citations(text: str) -> str:
    """Rewrite "path at line N" / "path at lines X-Y" into ``path:N`` form.

    Text rewrites are the only thing this does: validation still checks the path,
    the range, and the retrieved chunks, so an unsupported citation is dropped
    rather than invented.
    """

    def rewrite(match: re.Match[str]) -> str:
        path = match.group("path")
        start = match.group("start")
        end = match.group("end")
        label = f"{path}:{start}" + (f"-{end}" if end else "")
        return f"`{label}`" if match.group("open") else label

    return CITATION_VARIANT_RE.sub(rewrite, text)


@dataclass(slots=True)
class CitationIssue:
    citation: str
    reason: str

    def summary(self) -> dict:
        return {"citation": self.citation, "reason": self.reason}


@dataclass
class CitationReport:
    valid: list[str] = field(default_factory=list)
    invalid: list[CitationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.invalid

    def summary(self) -> dict:
        return {
            "valid": list(self.valid),
            "invalid": [issue.summary() for issue in self.invalid],
        }


@dataclass
class AnswerResult:
    question: str
    answer: str
    refused: bool
    retrieval: RetrievalResult
    citations: CitationReport
    model: ModelInfo | None = None
    trusted: bool = False

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "refused": self.refused,
            "trusted": self.trusted,
            "model": self.model.summary() if self.model else None,
            "citations": self.citations.summary(),
            "retrieval": self.retrieval.to_dict(),
        }


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def build_prompt(
    question: str, chunks: list[RetrievalChunk], *, scoped: bool = False
) -> str:
    parts = [f"Question: {question}", "", "Code excerpts:"]
    for index, chunk in enumerate(chunks, start=1):
        header = (
            f"[{index}] {chunk.path}:{chunk.start}-{chunk.end}  "
            f"{chunk.name} ({chunk.kind})"
        )
        block = [header]
        if chunk.docstring:
            doc = " ".join(chunk.docstring.split())
            block.append(f"Docstring: {doc[:400]}")
        if chunk.calls:
            block.append("Calls: " + ", ".join(chunk.calls))
        if chunk.siblings:
            block.append("Sibling signatures:")
            block.extend(f"  {signature}" for signature in chunk.siblings)
        block.append("```")
        block.append(chunk.text)
        block.append("```")
        parts.append("\n".join(block))
    parts.append("")
    if scoped:
        parts.append(
            "Give the short file overview described in the system prompt, using"
            " only these excerpts. Cite every code claim with the real path."
        )
    else:
        parts.append(
            "Answer the question using only these excerpts. Cite every code claim."
        )
    return "\n".join(parts)


def validate_citations(
    text: str,
    chunks: list[RetrievalChunk],
    file_lines: dict[str, int],
) -> tuple[str, CitationReport]:
    text = normalize_citations(text)
    report = CitationReport()
    spans: list[tuple[int, int, str]] = []

    for match in CITATION_RE.finditer(text):
        raw = match.group(0).strip("`")
        path = _normalize_path(match.group(1))
        start = int(match.group(2))
        end = int(match.group(3) or start)
        label = f"{path}:{start}" + (f"-{end}" if match.group(3) else "")

        reason = ""
        if path not in file_lines:
            reason = "unknown path"
        elif start < 1 or end < start:
            reason = "invalid range"
        elif end > file_lines[path]:
            reason = f"range exceeds file length ({file_lines[path]} lines)"
        elif not any(
            chunk.path == path and chunk.start <= end and start <= chunk.end
            for chunk in chunks
        ):
            reason = "symbol not in retrieved set"

        if reason:
            report.invalid.append(CitationIssue(citation=raw, reason=reason))
            spans.append((match.start(), match.end(), "[invalid citation removed]"))
        else:
            if label not in report.valid:
                report.valid.append(label)
            spans.append((match.start(), match.end(), label))

    clean = text
    for start_index, end_index, replacement in reversed(spans):
        clean = clean[:start_index] + replacement + clean[end_index:]

    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r"\s+([.,;:!?])", r"\1", clean)
    return clean.strip(), report


def prompt_for(
    question: str, retrieval: RetrievalResult, *, scope: str | None = None
) -> tuple[str, str]:
    """Return the system prompt and user prompt for one answer request."""
    scoped = scope is not None
    system = SCOPED_SYSTEM_PROMPT if scoped else SYSTEM_PROMPT
    return system, build_prompt(question, retrieval.chunks, scoped=scoped)


def finalize_answer(
    question: str,
    retrieval: RetrievalResult,
    file_lines: dict[str, int],
    text: str,
    *,
    model: ModelInfo | None = None,
) -> AnswerResult:
    """Turn raw model text into a validated answer (used by sync and streaming)."""
    if text.strip() == REFUSAL:
        return AnswerResult(
            question=question,
            answer=REFUSAL,
            refused=True,
            retrieval=retrieval,
            citations=CitationReport(),
            model=model,
        )

    clean, report = validate_citations(text, retrieval.chunks, file_lines)
    return AnswerResult(
        question=question,
        answer=clean,
        refused=False,
        retrieval=retrieval,
        citations=report,
        model=model,
        trusted=bool(report.valid) and report.ok,
    )


def answer(
    question: str,
    retrieval: RetrievalResult,
    file_lines: dict[str, int],
    *,
    model: ModelInfo | None = None,
    narrator: Callable[[str, str], str] | None = None,
    scope: str | None = None,
) -> AnswerResult:
    if retrieval.refused or not retrieval.chunks:
        return AnswerResult(
            question=question,
            answer=REFUSAL,
            refused=True,
            retrieval=retrieval,
            citations=CitationReport(),
            model=model,
        )

    system, prompt = prompt_for(question, retrieval, scope=scope)
    if narrator is None:
        if model is None:
            raise RuntimeError("no model configured")
        text = complete(system, prompt, model)
    else:
        text = narrator(system, prompt)

    return finalize_answer(question, retrieval, file_lines, text, model=model)
