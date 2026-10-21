"""The Python extractor (full).

Parses one Python source file with the ``tree-sitter`` ``python`` grammar and
emits the file's ``file``/``module``/``class``/``function``/``import`` nodes,
its intra-file ``defines`` edges (always ``exact``), and *unresolved* cross-file
edges for ``imports``/``inherits``/``calls``/``references``. An unresolved edge
carries its raw target string in ``dst_id`` (prefixed so the resolution pass can
recognise it) — the indexer binds it against the cross-file symbol table. The
extractor never resolves cross-file: by contract it sees only its own file.

Node-type names below were confirmed against the installed grammar pack
(``tree-sitter-language-pack`` 1.22.1 / ``tree-sitter`` 0.26.0): ``module``,
``import_statement``, ``import_from_statement`` (``dotted_name`` /
``aliased_import``), ``class_definition`` (name ``identifier``, bases
``argument_list``), ``function_definition``, ``block``, ``call`` (callee
``identifier`` or ``attribute``), ``attribute``, ``assignment``,
``return_statement``, ``parameters``, ``global_statement`` / ``nonlocal_statement``.
"""

from pathlib import PurePosixPath

from duckdb_codegraph.extractors.base import Extraction
from duckdb_codegraph.models import Edge, Node

# Unresolved-edge target marker. The resolution pass splits on this to recover
# the raw target string the extractor recorded; it is never a real node_id.
UNRESOLVED = "?"


def _module_qn(path: str) -> str:
    """Return the module qualified name for a stored POSIX path.

    The extension is dropped, separators become dots, and ``__init__`` collapses
    to the package: ``payments/charge.py`` -> ``payments.charge``;
    ``payments/__init__.py`` -> ``payments``.
    """
    pure = PurePosixPath(path)
    parts = list(pure.parts)
    stem = pure.stem
    if stem == "__init__":
        parts = parts[:-1]
    else:
        parts = [*parts[:-1], stem]
    return ".".join(parts)


def _node_id(kind: str, qualified_name: str, path: str, start_line: int) -> str:
    return f"{kind}:{qualified_name}@{path}:{start_line}"


def _unresolved_target(raw: str) -> str:
    return f"{UNRESOLVED}{raw}"


class PythonExtractor:
    """Extract graph nodes and edges from a single Python source file."""

    @property
    def language(self) -> str:
        return "python"

    def extract(self, text: str, path: str) -> Extraction:
        from tree_sitter_language_pack import get_parser

        parser = get_parser("python")
        tree = parser.parse(text.encode("utf-8"))
        builder = _PythonBuilder(path)
        builder.run(tree.root_node)
        return Extraction(nodes=builder.nodes, edges=builder.edges)


class _PythonBuilder:
    """Single-pass walk collecting nodes and unresolved edges for one file."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.module_qn = _module_qn(path)
        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        # De-dup set for (src_id, raw_target, kind) within the file.
        self._seen_edges: set[tuple[str, str, str]] = set()
        self._module_id = ""

    # -- helpers ---------------------------------------------------------

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
                language="python",
            )
        )
        return node_id

    def _add_defines(self, src_id: str, dst_id: str) -> None:
        self.edges.append(
            Edge(
                src_id=src_id,
                dst_id=dst_id,
                kind="defines",
                path=self.path,
                resolution="exact",
            )
        )

    def _add_unresolved(self, src_id: str, raw_target: str, kind: str) -> None:
        key = (src_id, raw_target, kind)
        if key in self._seen_edges:
            return
        self._seen_edges.add(key)
        self.edges.append(
            Edge(
                src_id=src_id,
                dst_id=_unresolved_target(raw_target),
                kind=kind,
                path=self.path,
                resolution="best_effort",
            )
        )

    # -- traversal -------------------------------------------------------

    def run(self, root) -> None:
        # file node spans the whole file; module node spans the whole file too.
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
                language="python",
            )
        )
        self._module_id = self._add_node(
            "module", self.module_qn or self.path, self.module_qn, root
        )
        self._add_defines(file_id, self._module_id)
        self._walk_body(root, scope_qn=self.module_qn, container_id=self._module_id)

    def _walk_body(self, node, scope_qn: str, container_id: str) -> None:
        """Walk the statements directly inside ``node`` (a module or block)."""
        for child in node.named_children:
            self._walk_statement(child, scope_qn, container_id)

    def _walk_statement(self, child, scope_qn: str, container_id: str) -> None:
        kind = child.type
        if kind == "class_definition":
            self._handle_class(child, scope_qn, container_id)
        elif kind == "function_definition":
            self._handle_function(child, scope_qn, container_id)
        elif kind == "import_statement":
            self._handle_import(child, aliased=False)
        elif kind == "import_from_statement":
            self._handle_import_from(child)
        else:
            self._walk_expressions(child, container_id)

    def _handle_class(self, node, scope_qn: str, container_id: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        qn = f"{scope_qn}.{name}" if scope_qn else name
        class_id = self._add_node("class", name, qn, node)
        self._add_defines(container_id, class_id)
        # Superclasses: the argument_list holds base identifiers/attributes.
        bases = node.child_by_field_name("superclasses")
        if bases is not None:
            for base in bases.named_children:
                base_name = self._attr_tail(base)
                if base_name:
                    self._add_unresolved(class_id, base_name, "inherits")
        body = node.child_by_field_name("body")
        if body is not None:
            self._walk_body(body, scope_qn=qn, container_id=class_id)

    def _handle_function(self, node, scope_qn: str, container_id: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        qn = f"{scope_qn}.{name}" if scope_qn else name
        func_id = self._add_node("function", name, qn, node)
        self._add_defines(container_id, func_id)
        params = self._param_names(node)
        body = node.child_by_field_name("body")
        if body is not None:
            # Nested defs append the defining function name to the scope.
            self._walk_function_body(body, scope_qn=qn, owner_id=func_id, bound=params)

    def _walk_function_body(
        self, body, scope_qn: str, owner_id: str, bound: set[str]
    ) -> None:
        for child in body.named_children:
            kind = child.type
            if kind == "class_definition":
                self._handle_class(child, scope_qn, owner_id)
            elif kind == "function_definition":
                self._handle_function(child, scope_qn, owner_id)
            elif kind == "import_statement":
                self._handle_import(child, aliased=False)
            elif kind == "import_from_statement":
                self._handle_import_from(child)
            else:
                self._walk_expressions(child, owner_id, bound=bound)

    # -- imports ---------------------------------------------------------

    def _handle_import(self, node, aliased: bool) -> None:
        # import M [as m][, N as n ...]
        for child in node.named_children:
            if child.type == "dotted_name":
                dotted = self._dotted(child)
                self._emit_import(dotted, dotted)
            elif child.type == "aliased_import":
                name_node = child.child_by_field_name("name")
                dotted = self._dotted(name_node) if name_node else ""
                if dotted:
                    self._emit_import(dotted, dotted)

    def _handle_import_from(self, node) -> None:
        # from M import N [as a], ...
        module_node = node.child_by_field_name("module_name")
        module_path = self._dotted(module_node) if module_node else ""
        module_range = module_node.byte_range if module_node else None
        names = [
            c
            for c in node.named_children
            if module_range is None or c.byte_range != module_range
        ]
        for child in names:
            if child.type == "dotted_name":
                imported = self._dotted(child)
                raw = f"{module_path}.{imported}" if module_path else imported
                self._emit_import(raw, raw)
            elif child.type == "aliased_import":
                name_node = child.child_by_field_name("name")
                imported = self._dotted(name_node) if name_node else ""
                if imported:
                    raw = f"{module_path}.{imported}" if module_path else imported
                    self._emit_import(raw, raw)
            elif child.type == "wildcard_import":
                continue

    def _emit_import(self, raw_target: str, import_name: str) -> None:
        import_qn = f"{self.module_qn}::import:{import_name}"
        # Use the module node's start line so the id stays stable and distinct.
        import_id = _node_id("import", import_qn, self.path, 1)
        if not any(n.node_id == import_id for n in self.nodes):
            self.nodes.append(
                Node(
                    node_id=import_id,
                    kind="import",
                    name=import_name.split(".")[-1],
                    qualified_name=import_qn,
                    path=self.path,
                    start_line=1,
                    end_line=1,
                    language="python",
                )
            )
        self._add_unresolved(import_id, raw_target, "imports")

    # -- expressions (calls / references) --------------------------------

    def _walk_expressions(
        self, node, src_id: str, bound: set[str] | None = None
    ) -> None:
        """Walk an expression subtree, emitting calls/references edges."""
        bound = bound or set()
        self._emit_calls_and_refs(node, src_id, bound)

    def _emit_calls_and_refs(self, node, src_id: str, bound: set[str]) -> None:
        kind = node.type
        if kind == "call":
            func = node.child_by_field_name("function")
            if func is not None:
                tail = self._attr_tail(func)
                if tail and tail not in bound:
                    self._add_unresolved(src_id, tail, "calls")
            args = node.child_by_field_name("arguments")
            if args is not None:
                for child in args.named_children:
                    self._emit_calls_and_refs(child, src_id, bound)
            return
        if kind == "attribute":
            # A non-callee attribute load a.b.n references the tail n.
            tail = self._attr_tail(node)
            if tail and tail not in bound:
                self._add_unresolved(src_id, tail, "references")
            return
        if kind == "identifier":
            name = self._text(node)
            if name not in bound:
                self._add_unresolved(src_id, name, "references")
            return
        if kind == "assignment":
            # The left-hand side is a binding occurrence; skip targets.
            right = node.child_by_field_name("right")
            if right is not None:
                self._emit_calls_and_refs(right, src_id, bound)
            return
        for child in node.named_children:
            self._emit_calls_and_refs(child, src_id, bound)

    # -- small parsing helpers -------------------------------------------

    def _dotted(self, node) -> str:
        if node is None:
            return ""
        if node.type == "dotted_name":
            return self._text(node)
        if node.type == "identifier":
            return self._text(node)
        return self._text(node)

    def _attr_tail(self, node) -> str:
        """Return the bare name for an identifier or the tail of an attribute."""
        if node is None:
            return ""
        if node.type == "identifier":
            return self._text(node)
        if node.type == "attribute":
            attr = node.child_by_field_name("attribute")
            if attr is not None:
                return self._text(attr)
            # fall back to the last named child
            named = node.named_children
            if named:
                return self._attr_tail(named[-1])
        return ""

    def _param_names(self, func_node) -> set[str]:
        names: set[str] = set()
        params = func_node.child_by_field_name("parameters")
        if params is None:
            return names
        for child in params.named_children:
            if child.type == "identifier":
                names.add(self._text(child))
            else:
                ident = child.child_by_field_name("name")
                if ident is not None and ident.type == "identifier":
                    names.add(self._text(ident))
                else:
                    first = child.named_children[0] if child.named_children else None
                    if first is not None and first.type == "identifier":
                        names.add(self._text(first))
        return names
