"""The JavaScript/TypeScript extractor (best-effort).

Parses one JS or TS source file with the ``tree-sitter`` ``javascript`` or
``typescript`` grammar (selected by language name) and emits a ``file`` node, a
single file-scoped ``module`` node, intra-file ``defines`` edges for top-level
declarations (``exact``), and *unresolved* cross-file edges. An ``imports`` edge
carries the raw specifier string for the indexer's specifier resolver; every
JS/TS ``imports``/``inherits``/``calls``/``references`` edge is best-effort
because JS/TS scoping and re-exports cannot be resolved reliably without type
information.

Node-type names below were confirmed against the installed grammar pack
(``tree-sitter-language-pack`` 1.22.1): ``program``, ``import_statement``
(specifier under ``string`` -> ``string_fragment``), ``export_statement``,
``function_declaration``, ``class_declaration`` (name ``identifier`` in JS,
``type_identifier`` in TS; heritage ``class_heritage`` wrapping ``extends`` in
JS and ``extends_clause`` in TS), ``lexical_declaration`` ->
``variable_declarator`` -> ``arrow_function``, ``method_definition``,
``call_expression``.
"""

from pathlib import PurePosixPath

from duckdb_graph.extractors.base import Extraction
from duckdb_graph.models import Edge, Node

UNRESOLVED = "?"


def _module_qn(path: str) -> str:
    """Return the file-scoped module qualified name (extension dropped).

    Slashes are kept as the file scope: ``components/Button.tsx`` ->
    ``components/Button``.
    """
    pure = PurePosixPath(path)
    parent = str(pure.parent)
    if parent in (".", ""):
        return pure.stem
    return f"{parent}/{pure.stem}"


def _node_id(kind: str, qualified_name: str, path: str, start_line: int) -> str:
    return f"{kind}:{qualified_name}@{path}:{start_line}"


def _unresolved_target(raw: str) -> str:
    return f"{UNRESOLVED}{raw}"


class JsTsExtractor:
    """Extract graph nodes and edges from a single JS/TS source file."""

    def __init__(self, language: str) -> None:
        self._language = language

    @property
    def language(self) -> str:
        return self._language

    def extract(self, text: str, path: str) -> Extraction:
        from tree_sitter_language_pack import get_parser

        parser = get_parser(self._language)
        tree = parser.parse(text.encode("utf-8"))
        builder = _JsTsBuilder(path, self._language)
        builder.run(tree.root_node)
        return Extraction(nodes=builder.nodes, edges=builder.edges)


class _JsTsBuilder:
    """Single-pass walk collecting nodes and unresolved edges for one file."""

    def __init__(self, path: str, language: str) -> None:
        self.path = path
        self.language = language
        self.module_qn = _module_qn(path)
        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        self._seen_edges: set[tuple[str, str, str]] = set()
        self._module_id = ""
        self._local_names: set[str] = set()

    @staticmethod
    def _line(node) -> tuple[int, int]:
        return node.start_point[0] + 1, node.end_point[0] + 1

    @staticmethod
    def _text(node) -> str:
        return node.text.decode("utf-8", "replace")

    def _add_node(self, kind: str, name: str, qualified_name: str, node) -> str:
        start, end = self._line(node)
        node_id = _node_id(kind, qualified_name, self.path, start)
        self.nodes.append(
            Node(
                node_id=node_id,
                kind=kind,
                name=name,
                qualified_name=qualified_name,
                path=self.path,
                start_line=start,
                end_line=end,
                language=self.language,
            )
        )
        return node_id

    def _add_edge(self, src_id: str, dst_id: str, kind: str, resolution: str) -> None:
        self.edges.append(
            Edge(
                src_id=src_id,
                dst_id=dst_id,
                kind=kind,
                path=self.path,
                resolution=resolution,
            )
        )

    def _add_unresolved(self, src_id: str, raw_target: str, kind: str) -> None:
        key = (src_id, raw_target, kind)
        if key in self._seen_edges:
            return
        self._seen_edges.add(key)
        self._add_edge(src_id, _unresolved_target(raw_target), kind, "best_effort")

    def run(self, root) -> None:
        start, end = self._line(root)
        file_qn = self.path
        file_id = _node_id("file", file_qn, self.path, start)
        self.nodes.append(
            Node(
                node_id=file_id,
                kind="file",
                name=PurePosixPath(self.path).name,
                qualified_name=file_qn,
                path=self.path,
                start_line=start,
                end_line=end,
                language=self.language,
            )
        )
        self._module_id = self._add_node("module", self.module_qn, self.module_qn, root)
        self._add_edge(file_id, self._module_id, "defines", "exact")
        # First pass: declarations (defines). Second pass: calls/references.
        for child in root.named_children:
            self._handle_top(child)
        for child in root.named_children:
            self._emit_calls_and_refs(child, self._module_id)

    def _handle_top(self, node) -> None:
        kind = node.type
        if kind == "import_statement":
            self._handle_import(node)
        elif kind == "export_statement":
            for child in node.named_children:
                self._handle_top(child)
        elif kind == "function_declaration":
            self._declare(node, "function")
        elif kind == "class_declaration":
            self._handle_class(node)
        elif kind == "lexical_declaration":
            self._handle_lexical(node)

    def _declare(self, node, kind: str) -> str | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = self._text(name_node)
        qn = f"{self.module_qn}.{name}"
        decl_id = self._add_node(kind, name, qn, node)
        self._add_edge(self._module_id, decl_id, "defines", "exact")
        self._local_names.add(name)
        return decl_id

    def _handle_class(self, node) -> None:
        class_id = self._declare(node, "class")
        if class_id is None:
            return
        heritage = node.child_by_field_name("superclass")
        if heritage is None:
            # Look for class_heritage / extends_clause among children.
            for child in node.named_children:
                if child.type in ("class_heritage", "extends_clause"):
                    heritage = child
                    break
        if heritage is not None:
            base = self._first_identifier(heritage)
            if base:
                self._add_unresolved(class_id, base, "inherits")

    def _handle_lexical(self, node) -> None:
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if name_node is None or value is None:
                continue
            if value.type in ("arrow_function", "function_expression", "function"):
                name = self._text(name_node)
                qn = f"{self.module_qn}.{name}"
                decl_id = self._add_node("function", name, qn, declarator)
                self._add_edge(self._module_id, decl_id, "defines", "exact")
                self._local_names.add(name)

    def _handle_import(self, node) -> None:
        source = node.child_by_field_name("source")
        if source is None:
            for child in node.named_children:
                if child.type == "string":
                    source = child
                    break
        if source is None:
            return
        specifier = self._string_value(source)
        if not specifier:
            return
        import_qn = f"{self.module_qn}::import:{specifier}"
        import_id = _node_id("import", import_qn, self.path, 1)
        if not any(n.node_id == import_id for n in self.nodes):
            self.nodes.append(
                Node(
                    node_id=import_id,
                    kind="import",
                    name=specifier,
                    qualified_name=import_qn,
                    path=self.path,
                    start_line=1,
                    end_line=1,
                    language=self.language,
                )
            )
        self._add_unresolved(import_id, specifier, "imports")

    def _emit_calls_and_refs(self, node, src_id: str) -> None:
        kind = node.type
        if kind == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None and func.type == "identifier":
                name = self._text(func)
                if name in self._local_names:
                    self._add_unresolved(src_id, name, "calls")
            args = node.child_by_field_name("arguments")
            if args is not None:
                for child in args.named_children:
                    self._emit_calls_and_refs(child, src_id)
            return
        for child in node.named_children:
            self._emit_calls_and_refs(child, src_id)

    # -- small parsing helpers -------------------------------------------

    def _string_value(self, node) -> str:
        for child in node.named_children:
            if child.type == "string_fragment":
                return self._text(child)
        text = self._text(node)
        return text.strip("'\"`")

    def _first_identifier(self, node) -> str:
        if node.type in ("identifier", "type_identifier"):
            return self._text(node)
        for child in node.named_children:
            found = self._first_identifier(child)
            if found:
                return found
        return ""
