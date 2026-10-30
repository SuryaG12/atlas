"""Extension -> language detection."""

from __future__ import annotations

from pathlib import Path

EXTENSIONS: dict[str, str] = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".scala": "Scala",
    ".c": "C",
    ".h": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".hh": "C++",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".m": "Objective-C",
    ".mm": "Objective-C",
    ".lua": "Lua",
    ".dart": "Dart",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hs": "Haskell",
    ".r": "R",
    ".jl": "Julia",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".ps1": "PowerShell",
    ".sql": "SQL",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "SCSS",
    ".less": "CSS",
    ".tf": "Terraform",
    ".proto": "Protobuf",
    ".graphql": "GraphQL",
    ".md": "Markdown",
    ".mdx": "Markdown",
    ".rst": "reStructuredText",
    ".json": "JSON",
    ".jsonc": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".ini": "INI",
    ".cfg": "INI",
    ".xml": "XML",
    ".csv": "CSV",
    ".txt": "Text",
}

FILENAMES: dict[str, str] = {
    "dockerfile": "Dockerfile",
    "makefile": "Makefile",
    "gemfile": "Ruby",
    "rakefile": "Ruby",
    "procfile": "Procfile",
    ".gitignore": "Config",
    ".gitattributes": "Config",
    ".dockerignore": "Config",
    ".editorconfig": "Config",
    ".npmrc": "Config",
    ".prettierrc": "Config",
    ".eslintrc": "Config",
}

# Languages whose lines count as "code" in the headline number.
NON_CODE = {
    "Markdown",
    "reStructuredText",
    "JSON",
    "YAML",
    "TOML",
    "INI",
    "XML",
    "CSV",
    "Text",
    "Config",
    "Other",
}


def detect_language(path: Path) -> str:
    name = path.name.lower()
    if name in FILENAMES:
        return FILENAMES[name]
    if name.startswith("dockerfile."):
        return "Dockerfile"
    return EXTENSIONS.get(path.suffix.lower(), "Other")


def is_code(language: str) -> bool:
    return language not in NON_CODE
