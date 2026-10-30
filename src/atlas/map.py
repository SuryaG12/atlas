"""Module / file dependency map built from the import graph."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .graph import _module_candidates, open_graph


@dataclass(slots=True)
class MapNode:
    key: str
    label: str
    files: int
    symbols: int


@dataclass(slots=True)
class MapEdge:
    src: str
    dst: str
    count: int


@dataclass
class MapData:
    level: str
    nodes: list[MapNode] = field(default_factory=list)
    edges: list[MapEdge] = field(default_factory=list)
    omitted: int = 0


def _group_of(path: str) -> str:
    parts = path.split("/")
    if len(parts) == 1:
        return "(root)"
    if parts[0] in {"src", "lib"} and len(parts) > 2:
        return "/".join(parts[:2])
    return parts[0]


def _node_label(path: str, files: int, symbols: int) -> str:
    label = f"{path}<br/>{files} file{'s' if files != 1 else ''}"
    if symbols:
        label += f" - {symbols} sym"
    return label


def build_map(root: Path, level: str = "package", limit: int = 30) -> MapData:
    conn = open_graph(Path(root))
    try:
        files = [
            (row["id"], row["path"], row["language"])
            for row in conn.execute("SELECT id, path, language FROM files")
        ]
        path_by_id = {file_id: path for file_id, path, _lang in files}
        language_by_id = {file_id: lang for file_id, _path, lang in files}
        all_paths = tuple(sorted(path_by_id.values()))

        symbol_counts = {
            row[0]: row[1]
            for row in conn.execute("SELECT file_id, COUNT(*) FROM symbols GROUP BY file_id")
        }

        def node_key(path: str) -> str:
            return path if level == "file" else _group_of(path)

        group_files: Counter[str] = Counter()
        group_symbols: Counter[str] = Counter()
        for file_id, path, _lang in files:
            key = node_key(path)
            group_files[key] += 1
            group_symbols[key] += symbol_counts.get(file_id, 0)

        import_edges: Counter[tuple[str, str]] = Counter()
        for row in conn.execute("SELECT file_id, module, level FROM imports"):
            file_id, module, import_level = row["file_id"], row["module"], row["level"]
            source_path = path_by_id.get(file_id)
            if not source_path:
                continue
            for candidate in _module_candidates(
                module,
                source_path,
                language_by_id.get(file_id, ""),
                import_level,
                all_paths,
            ):
                src_key = node_key(source_path)
                dst_key = node_key(candidate)
                if src_key == dst_key:
                    continue
                import_edges[(src_key, dst_key)] += 1

        degree: Counter[str] = Counter()
        for (src, dst), count in import_edges.items():
            degree[src] += count
            degree[dst] += count

        if level == "package" and not import_edges:
            return build_map(Path(root), "file", limit)

        keys = sorted(
            (
                key
                for key in group_files
                if degree.get(key, 0) > 0
                or (level == "file" and group_symbols.get(key, 0) > 0)
            ),
            key=lambda k: (-degree.get(k, 0), k),
        )
        if not keys:
            keys = sorted(group_files, key=lambda k: (-degree.get(k, 0), k))
        kept = keys[:limit]
        omitted = len(keys) - len(kept)
        kept_set = set(kept)

        data = MapData(level=level, omitted=omitted)
        data.nodes = [
            MapNode(
                key=key,
                label=_node_label(key, group_files[key], group_symbols[key]),
                files=group_files[key],
                symbols=group_symbols[key],
            )
            for key in kept
        ]
        data.edges = [
            MapEdge(src=src, dst=dst, count=count)
            for (src, dst), count in sorted(import_edges.items())
            if src in kept_set and dst in kept_set
        ]
        return data
    finally:
        conn.close()


def to_mermaid(data: MapData) -> str:
    if not data.nodes:
        return 'graph LR\n  empty["no internal imports found"]'
    ids = {node.key: f"n{index}" for index, node in enumerate(data.nodes)}
    lines = ["graph LR"]
    for node in data.nodes:
        lines.append(f'  {ids[node.key]}["{node.label}"]')
    for edge in data.edges:
        lines.append(f"  {ids[edge.src]} -->|{edge.count}| {ids[edge.dst]}")
    if data.omitted:
        lines.append(f"  %% {data.omitted} node(s) omitted")
    return "\n".join(lines)


def to_text(data: MapData) -> str:
    if not data.edges:
        return "(no internal import edges)"
    width = max(len(edge.src) for edge in data.edges)
    lines = [f"{edge.src.ljust(width)}  ->  {edge.dst}  ({edge.count})" for edge in data.edges]
    if data.omitted:
        lines.append(f"... {data.omitted} node(s) omitted")
    return "\n".join(lines)


def to_json(data: MapData) -> str:
    payload = {
        "level": data.level,
        "omitted": data.omitted,
        "nodes": [
            {
                "key": n.key,
                "label": n.label.replace("<br/>", " "),
                "files": n.files,
                "symbols": n.symbols,
            }
            for n in data.nodes
        ],
        "edges": [{"src": e.src, "dst": e.dst, "count": e.count} for e in data.edges],
    }
    return json.dumps(payload, indent=2)


def render(data: MapData, fmt: str) -> str:
    if fmt == "mermaid":
        return to_mermaid(data)
    if fmt == "json":
        return to_json(data)
    return to_text(data)
