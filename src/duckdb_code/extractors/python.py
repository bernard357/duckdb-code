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

import ast
from pathlib import PurePosixPath

from duckdb_code.extractors.base import Extraction
from duckdb_code.models import Edge, Node

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


# A docstring is kept up to this many characters: enough for the sentence that
# says what a symbol does (often past the first paragraph), short enough to
# keep the embedding text about the symbol.
DOCSTRING_CHARS = 300


# Raw-target marker for ``self.name`` inside a method: ``self::<class qn>::name``.
# The resolver binds it to the class's own method first, as Python's attribute
# lookup does, and falls back to the bare ``name`` (an inherited method).
SELF_TARGET = "self::"


def self_target(class_qn: str, name: str) -> str:
    """The raw target for ``self.name`` in a method of class ``class_qn``."""
    return f"{SELF_TARGET}{class_qn}::{name}"


def split_self_target(raw: str) -> tuple[str, str] | None:
    """Return ``(class_qn, name)`` for a :func:`self_target` raw, else None."""
    if not raw.startswith(SELF_TARGET):
        return None
    class_qn, _, name = raw[len(SELF_TARGET) :].rpartition("::")
    return (class_qn, name) if class_qn and name else None


def _join_import(module_path: str, imported: str) -> str:
    """The raw target of ``from <module_path> import <imported>``.

    ``from . import x`` is ``.x`` and ``from .pkg import x`` is ``.pkg.x``:
    a module part made only of dots is a relative level, not a name to join.
    """
    if not module_path:
        return imported
    if not module_path.strip("."):
        return f"{module_path}{imported}"
    return f"{module_path}.{imported}"


# Raw-target marker for ``<receiver>.<member>`` when the receiver names a known
# type or module: ``type::<flag>::<dotted name>::<member>``. The dotted name is
# an imported or module-level name (``json``, ``CodeStore``, ``pkg.util``) or
# the inferred class of a local (``store = CodeStore(...)``, ``s: CodeStore``);
# the flag is ``i`` when its first segment is bound by an import in the file,
# else ``n``. The resolver binds it through the module's names.
TYPE_TARGET = "type::"
# Raw-target marker for an attribute member on a receiver the extractor cannot
# type (``self.x.y``, a call result, an untyped local): ``attr::<member>``,
# resolved as a shared-name guess.
ATTR_TARGET = "attr::"


def type_target(flag: str, dotted: str, member: str) -> str:
    """The raw target of ``<dotted>.<member>`` for a typed receiver."""
    return f"{TYPE_TARGET}{flag}::{dotted}::{member}"


def split_type_target(raw: str) -> tuple[str, str, str] | None:
    """Return ``(flag, dotted, member)`` for a :func:`type_target` raw, else None."""
    if not raw.startswith(TYPE_TARGET):
        return None
    parts = raw[len(TYPE_TARGET) :].split("::")
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 and all(parts) else None


def attr_target(member: str) -> str:
    """The raw target of an attribute member on an untyped receiver."""
    return f"{ATTR_TARGET}{member}"


def split_attr_target(raw: str) -> str | None:
    """Return the member of an :func:`attr_target` raw, else None."""
    return raw[len(ATTR_TARGET) :] if raw.startswith(ATTR_TARGET) else None


# Statements that open a new scope: their bodies are not the function's locals.
_SCOPE_TYPES = frozenset({"function_definition", "class_definition", "lambda"})
# Tuple/list targets whose identifiers are all bound by the assignment.
_TARGET_GROUPS = frozenset(
    {
        "pattern_list",
        "tuple_pattern",
        "list_pattern",
        "tuple",
        "list",
        "list_splat_pattern",
    }
)


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
        return Extraction(
            nodes=builder.nodes, edges=builder.edges, docstrings=builder.docstrings
        )


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
        # Class node id -> class qualified name, so a method knows its class.
        self._class_qn_of: dict[str, str] = {}
        # (receiver parameter, class qn) while walking a method body, so
        # ``self.name`` resolves within the enclosing class first.
        self._receiver: tuple[str, str] | None = None
        # Names the file binds by import, and the current function's typed
        # locals (name -> class), for typed receivers.
        self._imported: set[str] = set()
        self._local_types: dict[str, str] = {}
        # node_id -> docstring of each class and function that has one.
        self.docstrings: dict[str, str] = {}

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
        self._imported = self._import_bound_names(root)
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
        elif kind == "decorated_definition":
            self._handle_decorated(child, scope_qn, container_id)
        elif kind == "import_statement":
            self._handle_import(child, aliased=False)
        elif kind == "import_from_statement":
            self._handle_import_from(child)
        else:
            self._walk_expressions(child, container_id)

    def _handle_decorated(self, node, scope_qn: str, container_id: str) -> None:
        """Handle a decorated class/function: extract the inner definition, and
        detect a route decorator (``@app.get("/x")`` / ``@router.post(...)`` /
        ``@app.route("/x")``) that marks the function as an HTTP route handler.

        The inner definition is extracted exactly as an undecorated one; the
        route scan is additive and only emits a ``route`` node when a decorator
        matches the Flask/FastAPI shape.
        """
        inner = node.child_by_field_name("definition")
        if inner is None:
            # Fall back to the last named child (the wrapped definition).
            named = node.named_children
            inner = named[-1] if named else None
        if inner is None:
            return
        # Applying a decorator named by a bare name (``@cache``,
        # ``@_described(server)``) uses it from the enclosing scope. An
        # attribute decorator (``@app.get``) would bind by its tail name to
        # every same-named method, so it is not recorded.
        for decorator in node.named_children:
            if decorator.type != "decorator" or not decorator.named_children:
                continue
            expr = decorator.named_children[0]
            if expr.type == "call":
                expr = expr.child_by_field_name("function") or expr
            if expr.type == "identifier":
                self._add_unresolved(container_id, self._text(expr), "calls")
        route = self._route_from_decorators(node)
        if inner.type == "class_definition":
            self._handle_class(inner, scope_qn, container_id)
        elif inner.type == "function_definition":
            handler_id = self._handle_function(inner, scope_qn, container_id)
            if route is not None and handler_id is not None:
                self._emit_route(route[0], route[1], inner)

    def _route_from_decorators(self, decorated) -> tuple[str, str] | None:
        """Return ``(method, path)`` for a Flask/FastAPI route decorator, or None.

        Recognizes ``@<obj>.<method>("/path")`` where method is an HTTP verb
        (get/post/put/patch/delete) and ``@<obj>.route("/path")`` (Flask, method
        defaults to GET). Only the first matching decorator is used.
        """
        verbs = {"get", "post", "put", "patch", "delete", "head", "options"}
        for child in decorated.named_children:
            if child.type != "decorator":
                continue
            call = child.named_children[0] if child.named_children else None
            if call is None or call.type != "call":
                continue
            fn = call.child_by_field_name("function")
            args = call.child_by_field_name("arguments")
            if fn is None or fn.type != "attribute" or args is None:
                continue
            attr = fn.child_by_field_name("attribute")
            method = self._text(attr).lower() if attr is not None else ""
            path = self._first_string_arg(args)
            if path is None:
                continue
            if method in verbs:
                return (method.upper(), path)
            if method == "route":
                return ("GET", path)
        return None

    def _first_string_arg(self, args) -> str | None:
        """Return the literal value of the first string argument, or None."""
        for arg in args.named_children:
            if arg.type == "string":
                return self._string_value(arg)
        return None

    @staticmethod
    def _string_value(node) -> str:
        """Strip quotes/prefix from a tree-sitter string node's text."""
        raw = node.text.decode("utf-8", errors="replace")
        # Drop a prefix like f/r/b, then the surrounding quotes.
        while raw and raw[0] not in ("'", '"'):
            raw = raw[1:]
        if len(raw) >= 2 and raw[0] in ("'", '"'):
            quote = raw[0]
            raw = raw.strip(quote)
        return raw

    def _emit_route(self, method: str, path: str, handler_node) -> None:
        """Emit a ``route`` node for a server-side HTTP route handler."""
        qn = f"{method} {path}"
        start, end = self._line(handler_node)
        route_id = _node_id("route", qn, self.path, start)
        self.nodes.append(
            Node(
                node_id=route_id,
                kind="route",
                name=path,
                qualified_name=qn,
                path=self.path,
                start_line=start,
                end_line=end,
                language="python",
            )
        )

    def _handle_class(self, node, scope_qn: str, container_id: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        qn = f"{scope_qn}.{name}" if scope_qn else name
        class_id = self._add_node("class", name, qn, node)
        self._class_qn_of[class_id] = qn
        self._add_defines(container_id, class_id)
        self._record_docstring(class_id, node.child_by_field_name("body"))
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

    def _handle_function(self, node, scope_qn: str, container_id: str) -> str | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = self._text(name_node)
        qn = f"{scope_qn}.{name}" if scope_qn else name
        func_id = self._add_node("function", name, qn, node)
        self._add_defines(container_id, func_id)
        self._record_docstring(func_id, node.child_by_field_name("body"))
        params = self._param_names(node)
        # A method (defined directly in a class body) resolves calls on its
        # first parameter (``self``/``cls``) within that class first.
        class_qn = self._class_qn_of.get(container_id)
        first = self._first_param(node) if class_qn is not None else None
        receiver = (first, class_qn) if first and class_qn else None
        body = node.child_by_field_name("body")
        if body is not None:
            # Names assigned anywhere in the body are locals, bound like params.
            bound = params | self._local_names(body)
            outer, self._receiver = self._receiver, receiver
            outer_types, self._local_types = (
                self._local_types,
                self._local_types_of(node, body),
            )
            try:
                # Nested defs append the defining function name to the scope.
                self._walk_function_body(
                    body, scope_qn=qn, owner_id=func_id, bound=bound
                )
            finally:
                self._receiver = outer
                self._local_types = outer_types
        return func_id

    def _walk_function_body(
        self, body, scope_qn: str, owner_id: str, bound: set[str]
    ) -> None:
        for child in body.named_children:
            kind = child.type
            if kind == "class_definition":
                self._handle_class(child, scope_qn, owner_id)
            elif kind == "function_definition":
                self._handle_function(child, scope_qn, owner_id)
            elif kind == "decorated_definition":
                # A decorated def inside a function (a tool registered in a
                # server factory, a route in an app factory) is a symbol too.
                self._handle_decorated(child, scope_qn, owner_id)
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
                raw = _join_import(module_path, self._dotted(child))
                self._emit_import(raw, raw)
            elif child.type == "aliased_import":
                name_node = child.child_by_field_name("name")
                imported = self._dotted(name_node) if name_node else ""
                if imported:
                    raw = _join_import(module_path, imported)
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
                raw = self._target_of(func, bound)
                if raw:
                    self._add_unresolved(src_id, raw, "calls")
            args = node.child_by_field_name("arguments")
            if args is not None:
                for child in args.named_children:
                    self._emit_calls_and_refs(child, src_id, bound)
            return
        if kind == "attribute":
            # A non-callee attribute load a.b.n references the tail n.
            raw = self._target_of(node, bound)
            if raw:
                self._add_unresolved(src_id, raw, "references")
            return
        if kind == "identifier":
            name = self._text(node)
            if name not in bound:
                self._add_unresolved(src_id, name, "references")
            return
        if kind == "keyword_argument":
            # `f(url=x)`: `url` names a parameter, not a symbol.
            value = node.child_by_field_name("value")
            if value is not None:
                self._emit_calls_and_refs(value, src_id, bound)
            return
        if kind == "assignment":
            # The left-hand side is a binding occurrence; skip targets.
            right = node.child_by_field_name("right")
            if right is not None:
                self._emit_calls_and_refs(right, src_id, bound)
            return
        for child in node.named_children:
            self._emit_calls_and_refs(child, src_id, bound)

    def _target_of(self, node, bound: set[str]) -> str:
        """The raw target a callee or attribute load names, or "" for none.

        A bare name bound in the function (a parameter or a local) names no
        module symbol. An attribute names its tail whatever the locals are
        (``self.links`` is not the local ``links``), and ``self.name`` in a
        method names the enclosing class's member first (:func:`self_target`).
        """
        if node.type == "identifier":
            name = self._text(node)
            return "" if name in bound else name
        tail = self._attr_tail(node)
        if node.type != "attribute" or not tail:
            return tail
        obj = node.child_by_field_name("object")
        if self._receiver is not None and obj is not None and obj.type == "identifier":
            receiver, class_qn = self._receiver
            if self._text(obj) == receiver:
                return self_target(class_qn, tail)
        dotted = self._dotted_chain(obj)
        if dotted is None:
            return attr_target(tail)
        head = dotted.split(".", 1)[0]
        if dotted in self._local_types:
            # A local whose every assignment is a call of one class.
            typed = self._local_types[dotted]
            flag = "i" if typed.split(".", 1)[0] in self._imported else "n"
            return type_target(flag, typed, tail)
        if head in bound:
            return attr_target(tail)
        return type_target("i" if head in self._imported else "n", dotted, tail)

    def _local_names(self, body) -> set[str]:
        """Names a function body binds: assignment, for, with-as, walrus targets.

        Nested functions, classes, and lambdas open their own scope and are
        skipped; names declared ``global`` or ``nonlocal`` are not local.
        """
        names: set[str] = set()
        declared: set[str] = set()
        stack = list(body.named_children)
        while stack:
            node = stack.pop()
            kind = node.type
            if kind in _SCOPE_TYPES:
                continue
            if kind in ("global_statement", "nonlocal_statement"):
                declared.update(
                    self._text(c) for c in node.named_children if c.type == "identifier"
                )
                continue
            if kind in (
                "assignment",
                "augmented_assignment",
                "for_statement",
                "for_in_clause",
            ):
                self._collect_targets(node.child_by_field_name("left"), names)
            elif kind == "as_pattern":
                self._collect_targets(node.child_by_field_name("alias"), names)
            elif kind == "named_expression":
                self._collect_targets(node.child_by_field_name("name"), names)
            stack.extend(node.named_children)
        return names - declared

    def _collect_targets(self, target, names: set[str]) -> None:
        """Add the identifiers an assignment target binds (not attributes)."""
        if target is None:
            return
        if target.type == "identifier":
            names.add(self._text(target))
        elif target.type in _TARGET_GROUPS or target.type == "as_pattern_target":
            for child in target.named_children:
                self._collect_targets(child, names)

    def _import_bound_names(self, root) -> set[str]:
        """Names any import in the file binds: ``import a.b`` binds ``a``."""
        names: set[str] = set()
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "import_statement":
                for child in node.named_children:
                    if child.type == "aliased_import":
                        alias = child.child_by_field_name("alias")
                        if alias is not None:
                            names.add(self._text(alias))
                    elif child.type == "dotted_name":
                        names.add(self._text(child).split(".", 1)[0])
            elif node.type == "import_from_statement":
                module = node.child_by_field_name("module_name")
                for child in node.named_children:
                    if module is not None and child.byte_range == module.byte_range:
                        continue
                    if child.type == "aliased_import":
                        alias = child.child_by_field_name("alias")
                        if alias is not None:
                            names.add(self._text(alias))
                    elif child.type == "dotted_name":
                        names.add(self._text(child).split(".")[-1])
            stack.extend(node.named_children)
        return names

    def _dotted_chain(self, node) -> str | None:
        """``a.b.c`` for a chain of identifiers, else None (calls, subscripts)."""
        if node is None:
            return None
        if node.type == "identifier":
            return self._text(node)
        if node.type == "attribute":
            obj = self._dotted_chain(node.child_by_field_name("object"))
            attr = node.child_by_field_name("attribute")
            if obj is not None and attr is not None:
                return f"{obj}.{self._text(attr)}"
        return None

    def _type_name(self, node) -> str | None:
        """The class an annotation names: ``Name``, ``a.Name``, ``Name | None``."""
        if node is None:
            return None
        if node.type == "type" and node.named_children:
            return self._type_name(node.named_children[0])
        if node.type == "binary_operator":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if right is not None and self._text(right) == "None":
                return self._type_name(left)
            if left is not None and self._text(left) == "None":
                return self._type_name(right)
            return None
        dotted = self._dotted_chain(node)
        if dotted and dotted.rsplit(".", 1)[-1][:1].isupper():
            return dotted
        return None

    def _local_types_of(self, func_node, body) -> dict[str, str]:
        """Locals and parameters whose class is known within the function.

        A parameter or variable annotated with a class name has that class; a
        local whose every assignment is a call of the same capitalized name
        (``store = CodeStore(...)``) has that class. Anything else (mixed
        assignments, loops, ``with ... as``, non-constructor values) is left
        untyped, so its attribute accesses stay guesses.
        """
        types: dict[str, set[str | None]] = {}
        params = func_node.child_by_field_name("parameters")
        for param in params.named_children if params is not None else []:
            if param.type in ("typed_parameter", "typed_default_parameter"):
                name_node = param.child_by_field_name("name")
                if name_node is None:
                    name_node = next(
                        (c for c in param.named_children if c.type == "identifier"),
                        None,
                    )
                if name_node is not None:
                    types.setdefault(self._text(name_node), set()).add(
                        self._type_name(param.child_by_field_name("type"))
                    )
        stack = list(body.named_children)
        while stack:
            node = stack.pop()
            if node.type in _SCOPE_TYPES:
                continue
            if node.type == "assignment":
                left = node.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    annotation = self._type_name(node.child_by_field_name("type"))
                    right = node.child_by_field_name("right")
                    constructed = None
                    if right is not None and right.type == "call":
                        constructed = self._type_name(
                            right.child_by_field_name("function")
                        )
                    types.setdefault(self._text(left), set()).add(
                        annotation or constructed
                    )
            elif node.type in (
                "for_statement",
                "for_in_clause",
                "as_pattern",
                "named_expression",
            ):
                for target in ("left", "alias", "name"):
                    bound_node = node.child_by_field_name(target)
                    if bound_node is not None and bound_node.type == "identifier":
                        types.setdefault(self._text(bound_node), set()).add(None)
            stack.extend(node.named_children)
        typed: dict[str, str] = {}
        for name, kinds in types.items():
            only = next(iter(kinds)) if len(kinds) == 1 else None
            if only is not None:
                typed[name] = only
        return typed

    def _record_docstring(self, node_id: str, body) -> None:
        """Keep a def's or class's docstring: whitespace-collapsed, capped."""
        if body is None or not body.named_children:
            return
        literal = body.named_children[0]
        # The grammar yields the docstring as a bare `string` in the block, or
        # wrapped in an `expression_statement` depending on its version.
        if literal.type == "expression_statement" and literal.named_children:
            literal = literal.named_children[0]
        if literal.type != "string":
            return
        try:
            value = ast.literal_eval(self._text(literal))
        except (ValueError, SyntaxError):
            return  # an f-string or an unusual literal: no docstring
        if isinstance(value, str) and value.strip():
            self.docstrings[node_id] = " ".join(value.split())[:DOCSTRING_CHARS]

    def _first_param(self, func_node) -> str | None:
        """The name of a function's first parameter (``self`` for a method)."""
        params = func_node.child_by_field_name("parameters")
        if params is None or not params.named_children:
            return None
        names = self._param_names_of([params.named_children[0]])
        return next(iter(names), None)

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
        params = func_node.child_by_field_name("parameters")
        if params is None:
            return set()
        return self._param_names_of(params.named_children)

    def _param_names_of(self, children) -> set[str]:
        """The names declared by a sequence of parameter nodes."""
        names: set[str] = set()
        for child in children:
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
