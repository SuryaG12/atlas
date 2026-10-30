"""Atlas CLI â€” M0/M1: walk, parse, graph, and report."""

from __future__ import annotations

import json as jsonlib
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from . import __version__
from .answer import REFUSAL, answer
from .diff import NoIndexError, compute_diff, render_text as render_diff, to_dict as diff_to_dict
from .entrypoints import find_entrypoints
from .graph import (
    SCHEMA_VERSION,
    graph_schema,
    open_graph,
    tree_sitter_status,
    update_graph,
)
from .manifest import build_manifest, read_manifest, write_manifest
from .map import build_map
from .map import render as render_map
from .narrate import LLMUnavailable, ModelInfo, NO_MODEL_MESSAGE, detect_model
from .report import render, render_graph, render_retrieval
from .retrieval import retrieve
from .walker import scan

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Local-first codebase explainer.",
)

PathArg = Annotated[Path, typer.Argument(help="Repository to inspect.")]
ExcludeOpt = Annotated[
    list[str],
    typer.Option("--exclude", "-x", help="Extra gitignore-style pattern to skip."),
]


class MapFormat(str, Enum):
    mermaid = "mermaid"
    text = "text"
    json = "json"


class MapLevel(str, Enum):
    package = "package"
    file = "file"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"atlas {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    """Atlas: index a repository, then explain it."""


def _validate(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        typer.secho(f"Path does not exist: {resolved}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if not resolved.is_dir():
        typer.secho(f"Not a directory: {resolved}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    return resolved


def _json_summary(root: Path, scan_result, entrypoints, manifest_path: Path | None, graph_stats=None) -> dict:
    summary = {
        "root": str(root),
        "files": len(scan_result.files),
        "lines": scan_result.total_lines,
        "code_lines": scan_result.code_lines,
        "bytes": scan_result.total_bytes,
        "seconds": round(scan_result.duration_s, 3),
        "entrypoints": [
            {"kind": e.kind, "path": e.path, "line": e.line, "detail": e.detail}
            for e in entrypoints
        ],
        "skipped": dict(scan_result.skipped),
        "manifest": str(manifest_path) if manifest_path else None,
    }
    if graph_stats is not None:
        summary["graph"] = {
            "files": graph_stats.files,
            "parsed": graph_stats.parsed,
            "skipped": graph_stats.skipped,
            "deleted": graph_stats.deleted,
            "added": graph_stats.added,
            "changed": graph_stats.changed,
            "full": graph_stats.full,
            "warning": graph_stats.warning,
            "symbols": graph_stats.symbols,
            "edges": graph_stats.edges,
            "resolved": graph_stats.resolved,
            "resolved_pct": round(graph_stats.resolved_pct, 1),
            "imports": graph_stats.imports,
            "parse_errors": graph_stats.parse_errors,
            "kinds": graph_stats.kinds,
            "seconds": round(graph_stats.seconds, 3),
        }
    return summary


@app.command("index")
def index_cmd(
    path: PathArg = Path("."),
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
    exclude: ExcludeOpt = [],
    include_vendored: Annotated[
        bool, typer.Option("--include-vendored", help="Index vendor/ and third_party/.")
    ] = False,
    use_gitignore: Annotated[
        bool, typer.Option("--gitignore/--no-gitignore", help="Honour .gitignore files.")
    ] = True,
    max_file_mb: Annotated[
        float, typer.Option("--max-file-mb", min=0.01, help="Skip files larger than this.")
    ] = 2.0,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only write the manifest.")] = False,
    graph_enabled: Annotated[
        bool,
        typer.Option("--graph/--no-graph", help="Parse code and build the symbol graph."),
    ] = True,
    full: Annotated[
        bool,
        typer.Option("--full", help="Force a complete rebuild instead of an incremental update."),
    ] = False,
) -> None:
    """Walk the repo, update the index, and print a summary."""
    root = _validate(path)
    previous_manifest = read_manifest(root)
    scan_result = scan(
        root,
        excludes=tuple(exclude),
        include_vendored=include_vendored,
        use_gitignore=use_gitignore,
        max_file_bytes=int(max_file_mb * 1024 * 1024),
    )
    entrypoints = find_entrypoints(root, scan_result.files)
    graph_stats = (
        update_graph(root, scan_result, previous_manifest, force_full=full)
        if graph_enabled
        else None
    )
    manifest = build_manifest(scan_result, entrypoints)
    manifest_path = write_manifest(root, manifest)

    if graph_stats is not None and graph_stats.warning:
        typer.secho(
            f"Warning: {graph_stats.warning}",
            fg=typer.colors.YELLOW,
            err=True,
        )

    if as_json:
        typer.echo(
            jsonlib.dumps(
                _json_summary(root, scan_result, entrypoints, manifest_path, graph_stats),
                indent=2,
            )
        )
        return
    if quiet:
        typer.echo(str(manifest_path))
        return
    try:
        manifest_label = str(manifest_path.relative_to(root))
    except ValueError:
        manifest_label = str(manifest_path)
    console = Console(highlight=False)
    render(
        scan_result,
        entrypoints,
        console,
        manifest_path=manifest_label,
    )
    if graph_stats is not None:
        render_graph(graph_stats, console)


@app.command("stats")
def stats_cmd(
    path: PathArg = Path("."),
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
    exclude: ExcludeOpt = [],
    include_vendored: Annotated[
        bool, typer.Option("--include-vendored", help="Include vendor/ and third_party/.")
    ] = False,
    use_gitignore: Annotated[
        bool, typer.Option("--gitignore/--no-gitignore", help="Honour .gitignore files.")
    ] = True,
    max_file_mb: Annotated[
        float, typer.Option("--max-file-mb", min=0.01, help="Skip files larger than this.")
    ] = 2.0,
) -> None:
    """Print repo statistics without writing a manifest."""
    root = _validate(path)
    scan_result = scan(
        root,
        excludes=tuple(exclude),
        include_vendored=include_vendored,
        use_gitignore=use_gitignore,
        max_file_bytes=int(max_file_mb * 1024 * 1024),
    )
    entrypoints = find_entrypoints(root, scan_result.files)

    if as_json:
        typer.echo(jsonlib.dumps(_json_summary(root, scan_result, entrypoints, None), indent=2))
        return
    render(scan_result, entrypoints, Console(highlight=False))


@app.command("map")
def map_cmd(
    path: PathArg = Path("."),
    output_format: Annotated[
        MapFormat,
        typer.Option("--format", "-f", help="mermaid | text | json"),
    ] = MapFormat.mermaid,
    level: Annotated[
        MapLevel,
        typer.Option("--level", "-l", help="Group by package or by file."),
    ] = MapLevel.package,
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=200, help="Maximum nodes to draw.")
    ] = 30,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write to a file instead of stdout.")
    ] = None,
) -> None:
    """Print the import graph as Mermaid, text, or JSON."""
    root = _validate(path)
    try:
        data = build_map(root, level.value, limit)
    except FileNotFoundError:
        typer.secho(
            "No graph found. Run `atlas index .` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    text = render_map(data, output_format.value)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        typer.echo(str(out))
        return
    typer.echo(text)


@app.command("ask")
def ask_cmd(
    question: Annotated[str, typer.Argument(help="Natural-language question about the repo.")],
    path: Annotated[Path, typer.Option("--path", "-p", help="Repository root.")] = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    no_llm: Annotated[
        bool,
        typer.Option("--no-llm", help="Show retrieval only: chunks, sources, fused ranks."),
    ] = False,
    top_k: Annotated[int, typer.Option("--top-k", min=1, max=20)] = 8,
    budget: Annotated[
        int, typer.Option("--budget", min=200, max=200000, help="Chunk token budget.")
    ] = 6000,
    min_score: Annotated[float, typer.Option("--min-score", min=0.0, max=1.0)] = 0.02,
    model_name: Annotated[
        str | None, typer.Option("--model", help="Override the local model name.")
    ] = None,
) -> None:
    """Ask a question about the repo, with cited answers from a local model."""
    root = _validate(path)
    try:
        conn = open_graph(root)
    except FileNotFoundError:
        typer.secho(
            "No graph found. Run `atlas index .` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        if graph_schema(conn) != SCHEMA_VERSION:
            typer.secho(
                "Graph index is out of date. Run `atlas index .` again.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        retrieval_result = retrieve(
            conn,
            question,
            top_k=top_k,
            budget=budget,
            min_score=min_score,
        )
        file_lines = {
            row["path"]: row["lines"]
            for row in conn.execute("SELECT path, lines FROM files")
        }
    finally:
        conn.close()

    console = Console(highlight=False)

    if no_llm:
        if as_json:
            payload = {"mode": "no-llm", **retrieval_result.to_dict()}
            typer.echo(jsonlib.dumps(payload, indent=2))
        else:
            render_retrieval(retrieval_result, console)
        return

    if retrieval_result.refused:
        if as_json:
            typer.echo(
                jsonlib.dumps(
                    {
                        "mode": "refused",
                        "answer": REFUSAL,
                        "refused": True,
                        "retrieval": retrieval_result.to_dict(),
                    },
                    indent=2,
                )
            )
        else:
            typer.echo(REFUSAL)
        return

    model = detect_model()
    if model is None:
        message = NO_MODEL_MESSAGE
        if as_json:
            typer.echo(
                jsonlib.dumps(
                    {
                        "mode": "error",
                        "error": message,
                        "retrieval": retrieval_result.to_dict(),
                    },
                    indent=2,
                )
            )
        else:
            typer.secho(message, fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=2)

    if model_name:
        model = ModelInfo(model.provider, model_name, model.endpoint)

    try:
        result_answer = answer(question, retrieval_result, file_lines, model=model)
    except LLMUnavailable as exc:
        message = f"Local model failed: {exc}. Use --no-llm to inspect retrieval."
        if as_json:
            typer.echo(jsonlib.dumps({"mode": "error", "error": message}, indent=2))
        else:
            typer.secho(message, fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=2)

    if as_json:
        typer.echo(jsonlib.dumps(result_answer.to_dict(), indent=2))
        return

    typer.echo(result_answer.answer)
    console.print()
    console.print(
        f"[dim]{len(result_answer.retrieval.chunks)} chunks | "
        f"{len(result_answer.citations.valid)} valid citations | "
        f"model {model.provider}/{model.model}[/dim]"
    )
    for issue in result_answer.citations.invalid:
        typer.secho(
            f"Invalid citation {issue.citation}: {issue.reason}",
            fg=typer.colors.RED,
            err=True,
        )
    if not result_answer.trusted:
        typer.secho(
            "Warning: answer has no validated citations.",
            fg=typer.colors.YELLOW,
            err=True,
        )


@app.command("diff")
def diff_cmd(
    path: PathArg = Path("."),
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Compare the working tree with the last indexed state."""
    root = _validate(path)
    try:
        result = compute_diff(root)
    except NoIndexError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if as_json:
        typer.echo(jsonlib.dumps(diff_to_dict(result), indent=2))
        return
    typer.echo(render_diff(result))


@app.command("web")
def web_cmd(
    path: Annotated[Path, typer.Option("--path", "-p", help="Repository root.")] = Path("."),
    port: Annotated[
        int, typer.Option("--port", min=1, max=65535, help="Port on 127.0.0.1.")
    ] = 8000,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Do not open a browser window.")
    ] = False,
) -> None:
    """Serve the read-only local web UI on 127.0.0.1."""
    root = _validate(path)
    try:
        conn = open_graph(root)
    except FileNotFoundError:
        typer.secho(
            "No graph found. Run `atlas index .` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        current = graph_schema(conn)
    finally:
        conn.close()
    if current != SCHEMA_VERSION:
        typer.secho(
            "Graph index is out of date. Run `atlas index .` again.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        import uvicorn
    except ImportError:
        typer.secho(
            "The web UI needs fastapi and uvicorn. Run `pip install -e .`.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    from .web import BIND_HOST, create_app

    url = f"http://{BIND_HOST}:{port}"
    typer.echo(f"Atlas web  {url}")
    typer.echo(f"Repo       {root}")
    typer.echo("Read-only  Ctrl+C to stop")
    if not no_browser:
        import threading
        import webbrowser

        timer = threading.Timer(0.8, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()

    try:
        uvicorn.run(create_app(root), host=BIND_HOST, port=port, log_level="info")
    except OSError as exc:
        typer.secho(
            f"Could not bind {BIND_HOST}:{port}: {exc}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
