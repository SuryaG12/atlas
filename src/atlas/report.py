"""Terminal rendering for the walker and graph layers."""

from __future__ import annotations

from dataclasses import dataclass

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__
from .answer import REFUSAL
from .entrypoints import EntryPoint, is_test_path
from .graph import GraphStats
from .retrieval import RetrievalResult
from .walker import ScanResult


@dataclass(slots=True)
class LanguageStat:
    language: str
    files: int
    lines: int
    bytes: int


def language_stats(scan: ScanResult) -> list[LanguageStat]:
    buckets: dict[str, LanguageStat] = {}
    for f in scan.files:
        stat = buckets.get(f.language)
        if stat is None:
            stat = buckets[f.language] = LanguageStat(f.language, 0, 0, 0)
        stat.files += 1
        stat.lines += f.lines
        stat.bytes += f.bytes
    return sorted(buckets.values(), key=lambda s: (-s.lines, s.language))


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _table(title: str | None = None) -> Table:
    return Table(
        box=box.SIMPLE_HEAD,
        title=title,
        title_justify="left",
        title_style="bold",
        pad_edge=False,
        show_edge=False,
    )


def render(
    scan: ScanResult,
    entrypoints: list[EntryPoint],
    console: Console,
    *,
    manifest_path: str | None = None,
    max_entrypoints: int = 12,
    max_files: int = 10,
) -> None:
    header = Text()
    header.append(f"Atlas {__version__}", style="bold cyan")
    header.append("  ")
    header.append(str(scan.root), style="dim")
    console.print(header)

    if not scan.files:
        console.print("[yellow]No text files found.[/yellow]")
        return

    test_files = [f for f in scan.files if is_test_path(f.path)]
    summary = Text()
    summary.append(f"{len(scan.files):,}", style="bold")
    summary.append(" files | ")
    summary.append(f"{scan.total_lines:,}", style="bold")
    summary.append(" lines (")
    summary.append(f"{scan.code_lines:,}", style="green")
    summary.append(" code) | ")
    summary.append(human_bytes(scan.total_bytes))
    summary.append(" | ")
    summary.append(f"{scan.duration_s:.2f}s", style="dim")
    console.print(summary)
    console.print()

    stats = language_stats(scan)
    table = _table("Languages")
    table.add_column("Language", style="cyan", no_wrap=True)
    table.add_column("Files", justify="right")
    table.add_column("Lines", justify="right")
    table.add_column("Share", justify="right", style="dim")
    for stat in stats[: max_files + 2]:
        share = stat.lines / scan.total_lines if scan.total_lines else 0
        table.add_row(
            stat.language,
            f"{stat.files:,}",
            f"{stat.lines:,}",
            f"{share * 100:5.1f}%",
        )
    remaining = stats[max_files + 2 :]
    if remaining:
        table.add_row(
            f"+{len(remaining)} more",
            f"{sum(s.files for s in remaining):,}",
            f"{sum(s.lines for s in remaining):,}",
            f"{sum(s.lines for s in remaining) / scan.total_lines * 100:5.1f}%",
            style="dim",
        )
    console.print(table)

    if entrypoints:
        console.print()
        table = _table("Entry points")
        table.add_column("Kind", style="magenta", no_wrap=True)
        table.add_column("Location", style="cyan", no_wrap=True)
        table.add_column("Detail", style="dim")
        for entry in entrypoints[:max_entrypoints]:
            table.add_row(
                entry.kind,
                f"{entry.path}:{entry.line}",
                entry.detail,
            )
        console.print(table)
        if len(entrypoints) > max_entrypoints:
            console.print(f"  [dim]+{len(entrypoints) - max_entrypoints} more[/dim]")

    largest = sorted(scan.files, key=lambda f: (-f.lines, f.path))[:max_files]
    if largest:
        console.print()
        table = _table("Largest files")
        table.add_column("Lines", justify="right")
        table.add_column("Language", style="cyan", no_wrap=True)
        table.add_column("Path")
        for f in largest:
            table.add_row(f"{f.lines:,}", f.language, f.path)
        console.print(table)

    console.print()
    test_share = (
        sum(f.lines for f in test_files) / scan.total_lines * 100 if scan.total_lines else 0
    )
    console.print(
        f"[bold]Tests[/bold]  {len(test_files):,} files | "
        f"{sum(f.lines for f in test_files):,} lines "
        f"[dim]({test_share:.0f}% of repo)[/dim]"
    )

    if scan.skipped:
        parts = [
            f"{count:,} {reason.replace('_', ' ')}"
            for reason, count in sorted(scan.skipped.items())
        ]
        console.print(f"[dim]Skipped  {' | '.join(parts)}[/dim]")
    if scan.gitignores:
        console.print(f"[dim]Read {scan.gitignores} .gitignore file(s)[/dim]")
    if manifest_path:
        console.print(f"[dim]Manifest  {manifest_path}[/dim]")


def render_graph(stats: GraphStats, console: Console) -> None:
    console.print()
    if not stats.files:
        console.print("[yellow]Graph  no files indexed[/yellow]")
        return

    line = Text()
    line.append("Graph", style="bold")
    line.append("  ")
    line.append(f"{stats.files:,}", style="bold")
    line.append(" files | ")
    line.append(f"{stats.parsed:,}", style="bold")
    line.append(" parsed | ")
    line.append(f"{stats.skipped:,}", style="bold")
    line.append(" skipped | ")
    line.append(f"{stats.deleted:,}", style="bold")
    line.append(" deleted | ")
    line.append(f"{stats.symbols:,}", style="bold")
    line.append(" symbols | ")
    line.append(f"{stats.chunks:,}", style="bold")
    line.append(" chunks | ")
    line.append(f"{stats.edges:,}", style="bold")
    line.append(" edges | ")
    line.append(f"{stats.internal_edges:,}", style="bold")
    line.append(" internal (")
    line.append(f"{stats.resolved_pct:.0f}% resolved", style="green")
    line.append(") | ")
    line.append(f"{stats.seconds:.2f}s", style="dim")
    console.print(line)

    if stats.full:
        console.print("[dim]Mode  full rebuild[/dim]")

    if stats.kinds:
        kinds = " | ".join(f"{kind} {count:,}" for kind, count in list(stats.kinds.items())[:6])
        console.print(f"[dim]{kinds}[/dim]")

    if stats.top_symbols:
        top = " | ".join(f"{name} ({count})" for name, count in stats.top_symbols[:5])
        console.print(f"[dim]Most called  {top}[/dim]")

    if stats.parse_errors:
        console.print(f"[yellow]{stats.parse_errors} file(s) had parse errors[/yellow]")
    console.print("[dim]Graph  .atlas/graph.db[/dim]")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _clip_left(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "..." + text[-(limit - 3) :]


def render_retrieval(result: RetrievalResult, console: Console) -> None:
    header = Text()
    header.append("Question  ", style="bold")
    header.append(result.question)
    console.print(header)

    tokens = ", ".join(result.tokens) if result.tokens else "(none)"
    console.print(f"[dim]Tokens  {tokens}[/dim]")
    console.print(
        f"[dim]Fused  best {result.best_score:.4f} | budget {result.budget_used} tokens"
        f" | exact symbol match: {result.exact_symbol_match}[/dim]"
    )

    if result.refused:
        console.print(f"[yellow]{REFUSAL} ({result.reason})[/yellow]")

    if result.chunks:
        table = _table("Chunks")
        table.add_column("#", justify="right", style="dim", width=2)
        table.add_column("Location", style="cyan", no_wrap=True, width=30)
        table.add_column("Symbol", no_wrap=True, width=22)
        table.add_column("Ranks", style="magenta", no_wrap=True, width=14)
        for index, chunk in enumerate(result.chunks, start=1):
            ranks = " ".join(
                f"{source[0]}#{rank}" for source, rank in sorted(chunk.ranks.items())
            )
            table.add_row(
                str(index),
                _clip_left(f"{chunk.path}:{chunk.start}-{chunk.end}", 30),
                _clip(chunk.name, 22),
                ranks,
            )
        console.print(table)

        for index, chunk in enumerate(result.chunks, start=1):
            console.print(
                f"[dim]#{index} {chunk.path}:{chunk.start}-{chunk.end} "
                f"{chunk.name} ({chunk.kind}) score {chunk.score:.4f}[/dim]"
            )
            if chunk.calls:
                console.print(f"[dim]   calls  {', '.join(chunk.calls[:8])}[/dim]")
            if chunk.siblings:
                console.print(
                    f"[dim]   siblings  {'; '.join(chunk.siblings[:3])}[/dim]"
                )
            preview = chunk.text.splitlines()
            for line in preview[:6]:
                console.print(f"   {line}")
            if len(preview) > 6:
                console.print("   ...")

    any_sources = any(result.sources.values())
    if any_sources:
        console.print()
        table = _table("Sources")
        table.add_column("Source", style="magenta", no_wrap=True, width=6)
        table.add_column("Rank", justify="right", width=4)
        table.add_column("Location", style="cyan", no_wrap=True, width=34)
        table.add_column("Symbol", no_wrap=True, width=24)
        for source in ("symbol", "fts", "graph"):
            for hit in result.sources.get(source, [])[:8]:
                table.add_row(
                    source,
                    str(hit.rank),
                    _clip_left(f"{hit.path}:{hit.start}-{hit.end}", 34),
                    _clip(hit.name, 24),
                )
        console.print(table)

