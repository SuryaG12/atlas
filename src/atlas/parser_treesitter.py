"""JavaScript / TypeScript / Go parsing via tree-sitter.

Python uses ``ast`` (see ``parser_python``). These grammars are optional: if the
wheels are unavailable for the running interpreter, the rest of Atlas still
works and only these languages are skipped.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .models import CallRef, ImportRef, ParsedFile, SymbolDef

try:  # pragma: no cover - import guard exercised on 3.15
    import tree_sitter_go
    import tree_sitter_javascript
    import tree_sitter_typescript
    from tree_sitter import Language, Node, Parser

    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False
    Node = object  # type: ignore[assignment,misc]

MAX_SIGNATURE = 180
FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}


@lru_cache(maxsize=None)
def _parser(language: str) -> "Parser":
    if language == "Go":
        lang = Language(tree_sitter_go.language())
    elif language == "TypeScript":
        lang = Language(tree_sitter_typescript.language_typescript())
    elif language == "TSX":
        lang = Language(tree_sitter_typescript.language_tsx())
    else:
        lang = Language(tree_sitter_javascript.language())
    return Parser(lang)


def _iter_descendants(node: "Node"):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _strip_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] in "'\"`" and text[-1] == text[0]:
        return text[1:-1]
    return text


class _Walker:
    def __init__(self, source: bytes, rel_path: str, language: str) -> None:
        self.source = source
        self.parsed = ParsedFile(path=rel_path, language=language)
        self.stack: list[SymbolDef] = []

    # -- helpers ---------------------------------------------------------
    def text(self, node: "Node | None") -> str:
        if node is None:
            return ""
        return self.source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def signature(self, node: "Node") -> str:
        first = self.text(node).split("\n", 1)[0].strip()
        first = first.rstrip("{").rstrip().rstrip(":").strip()
        first = " ".join(first.split())
        if len(first) > MAX_SIGNATURE:
            first = first[: MAX_SIGNATURE - 1] + "..."
        return first

    def leading_comment(self, node: "Node") -> str:
        parts: list[str] = []
        sibling = node.prev_sibling
        while sibling is not None and "comment" in sibling.type:
            parts.append(self.text(sibling).strip())
            sibling = sibling.prev_sibling
        return "\n".join(reversed(parts))[:1000]

    def add_symbol(
        self,
        node: "Node",
        name: str,
        kind: str,
        exported: bool,
        parent: str | None = None,
    ) -> SymbolDef:
        if parent is None and self.stack:
            parent = self.stack[-1].qualified_name
        qualified = f"{parent}.{name}" if parent else name
        symbol = SymbolDef(
            name=name,
            qualified_name=qualified,
            kind=kind,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=self.signature(node),
            parent=parent,
            exported=exported,
            docstring=self.leading_comment(node),
        )
        self.parsed.symbols.append(symbol)
        return symbol

    def call(self, node: "Node", name: str, receiver: str | None) -> None:
        self.parsed.calls.append(
            CallRef(
                name=name,
                line=node.start_point[0] + 1,
                receiver=receiver[:80] if receiver else None,
                enclosing=self.stack[-1].qualified_name if self.stack else None,
            )
        )

    # -- dispatch --------------------------------------------------------
    def walk(self, node: "Node", exported: bool = False) -> None:
        kind = node.type

        if kind == "export_statement":
            for child in node.children:
                self.walk(child, exported=True)
            return

        if kind in ("function_declaration", "generator_function_declaration"):
            self.function(node, exported)
            return

        if kind == "method_definition":
            self.function(node, exported, kind_override="method")
            return

        if kind == "class_declaration":
            self.class_(node, exported)
            return

        if kind in ("lexical_declaration", "variable_declaration"):
            self.variables(node, exported)
            return

        if kind == "interface_declaration":
            self.simple(node, "interface", exported)
            return
        if kind == "type_alias_declaration":
            self.simple(node, "type", exported)
            return
        if kind == "enum_declaration":
            self.simple(node, "enum", exported)
            return

        if kind in ("import_statement", "import_spec"):
            self.import_(node)
            # imports have no interesting children for us
            if kind == "import_statement":
                return

        if kind in ("call_expression", "new_expression"):
            self.call_expr(node)

        if kind == "function_declaration" and self.parsed.language == "Go":
            return

        for child in node.children:
            self.walk(child, exported)

    # -- handlers --------------------------------------------------------
    def function(self, node: "Node", exported: bool, kind_override: str = "function") -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            for child in node.children:
                self.walk(child, exported)
            return
        name = self.text(name_node)
        kind = kind_override
        if kind == "function" and self.parsed.language == "Go":
            exported = name[:1].isupper()
        symbol = self.add_symbol(node, name, kind, exported)
        self.stack.append(symbol)
        for child in node.children:
            if child is not name_node:
                self.walk(child, exported)
        self.stack.pop()

    def class_(self, node: "Node", exported: bool) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self.text(name_node)
        symbol = self.add_symbol(node, name, "class", exported)
        for child in node.children:
            if child.type == "class_heritage":
                for descendant in _iter_descendants(child):
                    if descendant.type == "identifier" and descendant is not name_node:
                        self.parsed.inherits.append(
                            (self.text(descendant), symbol.qualified_name, node.start_point[0] + 1)
                        )
                        break
        self.stack.append(symbol)
        for child in node.children:
            if child is not name_node:
                self.walk(child, exported)
        self.stack.pop()

    def variables(self, node: "Node", exported: bool) -> None:
        for declarator in node.children:
            if declarator.type != "variable_declarator":
                self.walk(declarator, exported)
                continue
            name_node = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if (
                name_node is not None
                and name_node.type == "identifier"
                and value is not None
                and value.type in FUNCTION_VALUES
            ):
                symbol = self.add_symbol(declarator, self.text(name_node), "function", exported)
                self.stack.append(symbol)
                for child in value.children:
                    self.walk(child, exported)
                self.stack.pop()
            else:
                for child in declarator.children:
                    self.walk(child, exported)

    def simple(self, node: "Node", kind: str, exported: bool) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        self.add_symbol(node, self.text(name_node), kind, exported)

    def import_(self, node: "Node") -> None:
        if node.type == "import_spec":  # Go
            path = node.child_by_field_name("path")
            if path is not None:
                alias_node = node.child_by_field_name("name")
                self.parsed.imports.append(
                    ImportRef(
                        module=_strip_quotes(self.text(path)),
                        alias=self.text(alias_node) if alias_node is not None else None,
                        line=node.start_point[0] + 1,
                    )
                )
            return

        source = node.child_by_field_name("source")
        if source is None:
            return
        module = _strip_quotes(self.text(source))
        line = node.start_point[0] + 1
        self.parsed.imports.append(ImportRef(module=module, line=line))

        for descendant in _iter_descendants(node):
            if descendant.type == "import_specifier":
                name_node = descendant.child_by_field_name("name")
                alias_node = descendant.child_by_field_name("alias")
                if name_node is not None:
                    self.parsed.imports.append(
                        ImportRef(
                            module=module,
                            name=self.text(name_node),
                            alias=self.text(alias_node) if alias_node is not None else None,
                            line=line,
                        )
                    )

    def call_expr(self, node: "Node") -> None:
        target = node.child_by_field_name("function")
        if target is None:
            target = node.child_by_field_name("constructor")
        if target is None:
            for child in node.children:
                self.walk(child)
            return

        name: str | None = None
        receiver: str | None = None
        if target.type in ("identifier", "property_identifier", "field_identifier"):
            name = self.text(target)
        elif target.type == "member_expression":
            prop = target.child_by_field_name("property")
            obj = target.child_by_field_name("object")
            name = self.text(prop) if prop is not None else None
            receiver = self.text(obj) if obj is not None else None
        elif target.type == "selector_expression":  # Go
            field_node = target.child_by_field_name("field")
            operand = target.child_by_field_name("operand")
            name = self.text(field_node) if field_node is not None else None
            receiver = self.text(operand) if operand is not None else None

        if name:
            self.call(node, name, receiver)
        for child in node.children:
            if child is not target:
                self.walk(child)


def parse_treesitter(path: Path, rel_path: str, language: str) -> ParsedFile:
    if not AVAILABLE:  # pragma: no cover
        return ParsedFile(path=rel_path, language=language, errors=1)
    try:
        source = path.read_bytes()
    except OSError:
        return ParsedFile(path=rel_path, language=language, errors=1)

    tree = _parser(language).parse(source)
    walker = _Walker(source, rel_path, language)
    walker.walk(tree.root_node)
    if tree.root_node.has_error:
        walker.parsed.errors = 1
    return walker.parsed
