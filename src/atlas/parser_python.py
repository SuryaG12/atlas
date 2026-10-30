"""Python parsing via the stdlib ``ast`` module.

No tree-sitter needed for Python, which means Atlas keeps working on
interpreters where the grammar wheels aren't available yet.
"""

from __future__ import annotations

import ast
from pathlib import Path

from .models import CallRef, ImportRef, ParsedFile, SymbolDef

MAX_SIGNATURE = 180


def _signature(node: ast.AST, source_lines: list[str], name: str) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        try:
            params = ast.unparse(node.args)
        except Exception:  # pragma: no cover - defensive
            params = ""
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        signature = f"{prefix} {name}({params})"
    elif isinstance(node, ast.ClassDef):
        try:
            bases = ", ".join(ast.unparse(base) for base in node.bases)
        except Exception:  # pragma: no cover - defensive
            bases = ""
        signature = f"class {name}({bases})" if bases else f"class {name}"
    else:  # pragma: no cover - defensive
        index = max(node.lineno - 1, 0)
        signature = source_lines[index].strip() if index < len(source_lines) else name

    signature = " ".join(signature.split())
    if len(signature) > MAX_SIGNATURE:
        signature = signature[: MAX_SIGNATURE - 3] + "..."
    return signature


class _Visitor(ast.NodeVisitor):
    def __init__(self, source_lines: list[str]) -> None:
        self.source_lines = source_lines
        self.stack: list[SymbolDef] = []
        self.parsed = ParsedFile(path="", language="Python")

    @property
    def enclosing(self) -> str | None:
        return self.stack[-1].qualified_name if self.stack else None

    def _add_symbol(self, node: ast.AST, name: str, kind: str) -> SymbolDef:
        parent = self.stack[-1] if self.stack else None
        qualified = f"{parent.qualified_name}.{name}" if parent else name
        start = getattr(node, "lineno", 1)
        decorators = getattr(node, "decorator_list", None) or []
        if decorators:
            start = min(start, min(d.lineno for d in decorators))
        docstring = ""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            docstring = ast.get_docstring(node) or ""
        symbol = SymbolDef(
            name=name,
            qualified_name=qualified,
            kind=kind,
            start_line=start,
            end_line=getattr(node, "end_lineno", None) or start,
            signature=_signature(node, self.source_lines, name),
            parent=parent.qualified_name if parent else None,
            exported=not name.startswith("_"),
            docstring=docstring,
        )
        self.parsed.symbols.append(symbol)
        return symbol

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        symbol = self._add_symbol(node, node.name, "class")
        for base in node.bases:
            if isinstance(base, ast.Name):
                self.parsed.inherits.append((base.id, symbol.qualified_name, node.lineno))
            elif isinstance(base, ast.Attribute):
                self.parsed.inherits.append((base.attr, symbol.qualified_name, node.lineno))
        self.stack.append(symbol)
        self.generic_visit(node)
        self.stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        kind = "method" if self.stack and self.stack[-1].kind == "class" else "function"
        symbol = self._add_symbol(node, node.name, kind)
        self.stack.append(symbol)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.parsed.imports.append(
                ImportRef(
                    module=alias.name,
                    name=None,
                    alias=alias.asname,
                    line=node.lineno,
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            self.parsed.imports.append(
                ImportRef(
                    module=module,
                    name=alias.name,
                    alias=alias.asname,
                    line=node.lineno,
                    level=node.level or 0,
                )
            )

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name: str | None = None
        receiver: str | None = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
            try:
                receiver = ast.unparse(func.value)[:80]
            except Exception:  # pragma: no cover - defensive
                receiver = None
        if name:
            self.parsed.calls.append(
                CallRef(
                    name=name,
                    line=node.lineno,
                    receiver=receiver,
                    enclosing=self.enclosing,
                )
            )
        self.generic_visit(node)


def parse_python(path: Path, rel_path: str) -> ParsedFile:
    try:
        data = path.read_bytes()
    except OSError:
        return ParsedFile(path=rel_path, language="Python", errors=1)

    try:
        tree = ast.parse(data, filename=rel_path)
    except (SyntaxError, ValueError):
        return ParsedFile(path=rel_path, language="Python", errors=1)

    source = data.decode("utf-8", errors="replace")
    visitor = _Visitor(source.splitlines())
    try:
        visitor.visit(tree)
    except RecursionError:  # pragma: no cover - pathological files
        return ParsedFile(path=rel_path, language="Python", errors=1)

    parsed = visitor.parsed
    parsed.path = rel_path
    return parsed
