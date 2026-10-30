"""Manifest: the M0 artifact the rest of the pipeline will build on."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .entrypoints import EntryPoint, is_test_path
from .walker import ScanResult

SCHEMA_VERSION = 1
MANIFEST_DIR = ".atlas"
MANIFEST_NAME = "manifest.json"


def build_manifest(scan: ScanResult, entrypoints: list[EntryPoint]) -> dict:
    test_files = [f for f in scan.files if is_test_path(f.path)]
    return {
        "schema": SCHEMA_VERSION,
        "tool": f"atlas {__version__}",
        "root": str(scan.root),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "files": len(scan.files),
            "lines": scan.total_lines,
            "code_lines": scan.code_lines,
            "bytes": scan.total_bytes,
            "test_files": len(test_files),
            "test_lines": sum(f.lines for f in test_files),
            "index_seconds": round(scan.duration_s, 3),
            "skipped": dict(scan.skipped),
        },
        "entrypoints": [
            {
                "kind": e.kind,
                "path": e.path,
                "line": e.line,
                "detail": e.detail,
                "score": e.score,
            }
            for e in entrypoints
        ],
        "files": [
            {
                "path": f.path,
                "language": f.language,
                "lines": f.lines,
                "bytes": f.bytes,
                "sha256": f.sha256,
            }
            for f in scan.files
        ],
    }


def write_manifest(root: Path, manifest: dict) -> Path:
    target_dir = Path(root) / MANIFEST_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / MANIFEST_NAME
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return target
