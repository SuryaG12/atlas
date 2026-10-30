"""Gitignore-aware ignore rules.

One global ruleset for defaults the tool never wants to index, plus a stack of
per-directory .gitignore rulesets discovered while walking. Later rulesets
(deeper directories) win, matching git's own precedence.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec

BASE_PATTERNS = [
    # vcs / tooling
    ".git/",
    ".hg/",
    ".svn/",
    ".atlas/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".tox/",
    # build output
    "dist/",
    "build/",
    "target/",
    "out/",
    "coverage/",
    ".next/",
    ".nuxt/",
    ".cache/",
    ".venv/",
    "venv/",
    "env/",
    "ENV/",
    "node_modules/",
    "bower_components/",
    "Pods/",
    "DerivedData/",
    # binaries / artefacts
    "*.py[cod]",
    "*.so",
    "*.dylib",
    "*.dll",
    "*.exe",
    "*.o",
    "*.a",
    "*.class",
    "*.jar",
    "*.war",
    "*.min.js",
    "*.min.css",
    "*.map",
    # lockfiles: zero signal for code explanation, lots of lines
    "*.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
]

VENDORED_PATTERNS = ["vendor/", "third_party/", "third-party/"]


@dataclass(slots=True)
class RuleSet:
    base: Path
    spec: GitIgnoreSpec


def _last_check(spec: GitIgnoreSpec, rel: str, is_dir: bool) -> bool | None:
    """Evaluate a spec, honouring later rules over earlier ones.

    Returns True if ignored, False if explicitly un-ignored, None for no match.
    """
    candidates = [rel]
    if is_dir:
        candidates.append(rel + "/")
    best: bool | None = None
    best_index = -1
    for candidate in candidates:
        result = spec.check_file(candidate)
        if result.include is not None and result.index >= best_index:
            best_index = result.index
            best = result.include
    return best


class IgnoreRules:
    def __init__(
        self,
        root: Path,
        *,
        excludes: tuple[str, ...] = (),
        include_vendored: bool = False,
        use_gitignore: bool = True,
    ) -> None:
        self.root = root
        self.use_gitignore = use_gitignore
        patterns = [
            p
            for p in BASE_PATTERNS
            if include_vendored or p not in VENDORED_PATTERNS
        ]
        patterns.extend(excludes)
        self.global_spec = GitIgnoreSpec.from_lines(patterns)
        self.nested: list[RuleSet] = []
        self.gitignore_count = 0

    def load_gitignore(self, directory: Path) -> None:
        if not self.use_gitignore:
            return
        path = directory / ".gitignore"
        if not path.is_file():
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        lines = [
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not lines:
            return
        self.nested.append(RuleSet(directory, GitIgnoreSpec.from_lines(lines)))
        self.gitignore_count += 1

    def is_ignored(self, path: Path, *, is_dir: bool) -> bool:
        rel = path.relative_to(self.root).as_posix()
        if self.global_spec.match_file(rel) or (is_dir and self.global_spec.match_file(rel + "/")):
            return True

        ignored = False
        for ruleset in self.nested:
            try:
                sub = path.relative_to(ruleset.base).as_posix()
            except ValueError:
                continue
            result = _last_check(ruleset.spec, sub, is_dir)
            if result is not None:
                ignored = result
        return ignored
