"""Index a folder of source code into a code-graph store.

Walks the folder, routes each file to its language extractor, resolves the
cross-file edges against an in-memory symbol table (a pure function — no
DuckDB), and writes the resolved graph into the store.

v1 build mode is a FULL REBUILD by table recreation: at run start the three
graph tables and ``files`` are dropped+recreated and ``meta`` cleared (then the
read-write indexes are recreated via the store's ``_ensure_schema``), so a run
against an existing database yields exactly the same content as a run against a
fresh file. The per-file delete-then-insert (:func:`_rebuild_file`) is reserved
for the incremental seam and is NOT called by the full-build path; it is proven
in isolation by the seam test.
"""

import datetime
import logging
import os
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    import pathspec

from duckdb_code.embedders import resolve_embedder
from duckdb_code.etag import compute_etag
from duckdb_code.extractors.base import Extraction
from duckdb_code.extractors.markdown import _slug
from duckdb_code.extractors.python import UNRESOLVED
from duckdb_code.extractors.registry import detect_language, resolve_extractor
from duckdb_code.models import (
    CONFIDENCE_EXACT,
    CONFIDENCE_IMPORT,
    CONFIDENCE_SAME_MODULE,
    CONFIDENCE_SUFFIX,
    Edge,
    Node,
    resolution_of,
)
from duckdb_code.store import INDEX_DDL, SCHEMA_DDL, SCHEMA_VERSION, CodeStore

logger = logging.getLogger(__name__)

# The cross-file edge kinds, resolved by the resolution pass.
_IMPORT_KINDS = ("imports",)
_SCOPED_KINDS = ("inherits", "calls", "references")

# Maximum number of in-set symbols a bare-name suffix match may bind to before
# the scoped edge is treated as unresolvable noise and dropped. A common method
# or function name (``run``, ``handler``, ``__init__``) shares its last segment
# with symbols in many unrelated modules; binding to all of them fabricates a
# dense best_effort fan-out that is noise and does not scale. Above this cap the
# suffix match carries no useful dependency signal, so the edge is dropped.
_SUFFIX_FANOUT_CAP = 20

# The fixed extension order the JS/TS specifier resolver tries (NFR 2).
_JS_TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_JS_TS_LANGUAGES = frozenset({"javascript", "typescript"})

# The Markdown cross-page edge kinds, resolved by the Markdown link pass.
_MARKDOWN_LINK_KINDS = ("links_to", "embeds")
# The Markdown page->tag edge kind, resolved by the Markdown tag pass.
_MARKDOWN_TAG_KIND = "tagged"
# The stored-path suffixes a Markdown link target may point at a page by.
_MARKDOWN_EXTENSIONS = (".md", ".markdown")

# Edge kinds a test's reachability is derived from: what a test calls, imports,
# or references is what it exercises. ``inherits`` is excluded (a test class
# subclassing a base does not "test" the base) and ``defines`` is structural.
_TEST_REACH_KINDS = frozenset({"calls", "references", "imports"})

# A path is a test file when its basename matches ``test_*`` / ``*_test`` (any
# extension) or ``*.test.*`` / ``*.spec.*``, or when any path segment is a
# conventional test directory. Mirrors the pytest / jest / vitest conventions
# the supported languages use.
_TEST_DIR_SEGMENTS = frozenset({"tests", "test", "__tests__", "spec", "__mocks__"})

# Directories never descended into by default (pruned in place during the walk
# by exact basename). The whole subtree is skipped, so nothing under them is
# ever stat'd, read, or counted — a pruned tree contributes 0 to every counter.
_DEFAULT_IGNORED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "vendor",
        "dist",
        "build",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

# Files strictly larger than this (in bytes) are dropped by default. The bound
# is exclusive: a file of exactly 1_000_000 bytes is KEPT; 1_000_001 is dropped.
_DEFAULT_MAX_FILE_BYTES = 1_000_000

# Optional per-folder ignore files, parsed (when present) with gitignore
# semantics and applied relative to the indexed folder. A missing file is a
# no-op (contributes no patterns).
_IGNORE_FILES = (".gitignore", ".duckdbgraphignore")


class IndexReport(BaseModel):
    """Outcome of an :func:`index_folder` run.

    ``files``/``indexed``/``skipped``/``nodes``/``edges``/``languages`` are
    whole-graph totals of the FINAL database state, so a no-op incremental run
    still reports the true graph totals (not zero). ``mode`` is ``"full"`` for a
    full rebuild (empty DB or ``rebuild=True``) and ``"incremental"`` otherwise.
    ``rebuilt`` counts the files re-extracted and rewritten this run (0 on a
    no-change incremental run); ``deleted`` counts the files whose rows were
    dropped because they vanished from disk.

    ``skipped`` is a single combined count of every file the walk encountered
    but did not index: files of an unsupported extension, files dropped by an
    ignore rule (over the size cap, or matched by ``.gitignore`` /
    ``.duckdbgraphignore``), and unreadable files. Whole directories excluded by
    default (``node_modules``, ``.venv``, ``.git``, build/cache dirs, etc.) are
    PRUNED before descent, so the files inside them are never walked and
    contribute 0 to ``skipped`` (and to every other counter) — only files the
    walk actually reached and dropped are counted.
    """

    files: int = 0
    indexed: int = 0
    skipped: int = 0
    nodes: int = 0
    edges: int = 0
    languages: dict[str, int] = {}
    mode: str = "full"
    rebuilt: int = 0
    deleted: int = 0


def _raw_target(dst_id: str) -> str:
    """Recover the raw target string from an unresolved edge ``dst_id``."""
    return dst_id[len(UNRESOLVED) :] if dst_id.startswith(UNRESOLVED) else dst_id


def _is_unresolved(edge: Edge) -> bool:
    return edge.dst_id.startswith(UNRESOLVED)


def _module_of_path(path: str, by_path_module: dict[str, str]) -> str | None:
    return by_path_module.get(path)


def resolve_edges(nodes: list[Node], edges: list[Edge]) -> list[Edge]:
    """Resolve the unresolved cross-file edges against a symbol table.

    Pure function (no DuckDB): deterministic and unit-testable in isolation. The
    order is load-bearing — ``by_qn``/``by_module`` are built first, the
    module-local-defines half of ``by_local`` is SEEDED (fix 1, a pure string
    test), then Phase A resolves ``imports`` (each binding also populating the
    import half of ``by_local``), then Phase B resolves ``inherits`` and
    ``calls``/``references`` reading the now-complete ``by_local`` WITHOUT
    mutating it.

    ``defines`` edges pass through unchanged (always ``exact``). An unresolved
    edge that binds to zero in-set nodes is dropped (never fabricated).
    """
    by_qn: dict[str, list[str]] = {}
    by_module: dict[str, str] = {}
    by_path_module: dict[str, str] = {}
    node_by_id: dict[str, Node] = {}
    js_ts_modules: dict[str, str] = {}

    for node in nodes:
        node_by_id[node.node_id] = node
        by_qn.setdefault(node.qualified_name, []).append(node.node_id)
        if node.kind == "module":
            by_module[node.qualified_name] = node.node_id
            by_path_module[node.path] = node.qualified_name
            if node.language in _JS_TS_LANGUAGES:
                js_ts_modules[node.qualified_name] = node.node_id

    # Index node ids by the LAST dotted segment of their qualified_name, so the
    # scoped suffix fallback (:func:`_resolve_scoped`) is an O(1) lookup instead
    # of a full ``by_qn`` scan per edge (the O(scoped_edges × distinct_qns)
    # hotspot). Built from ``by_qn`` in its insertion order, appending each qn's
    # ids in order, so the resulting lists are byte-identical to the old
    # comprehension ``[nid for qn, nids in by_qn.items() ... if last == raw]``.
    by_last_segment: dict[str, list[str]] = {}
    for qn, nids in by_qn.items():
        by_last_segment.setdefault(qn.rsplit(".", 1)[-1], []).extend(nids)

    # Seed half of by_local (fix 1): PURE STRING TEST. A class/function node
    # whose qualified_name == f"{m_qn}.{name}" is a module-top-level define
    # (its module qn is the immediate dotted prefix, no intervening scope
    # segment). Methods and nested functions are excluded because an
    # intervening scope segment sits between the module qn and the name.
    by_local: dict[tuple[str, str], list[str]] = {}
    for node in nodes:
        if node.kind not in ("class", "function"):
            continue
        m_qn = by_path_module.get(node.path)
        if m_qn is None:
            continue
        if node.qualified_name == f"{m_qn}.{node.name}":
            by_local.setdefault((m_qn, node.name), []).append(node.node_id)

    resolved: list[Edge] = []

    # defines edges pass through untouched.
    for edge in edges:
        if edge.kind == "defines":
            resolved.append(edge)

    # Phase A: imports. Each successful binding also inserts the bound local
    # name into the import half of by_local.
    for edge in edges:
        if edge.kind not in _IMPORT_KINDS or not _is_unresolved(edge):
            continue
        src = node_by_id.get(edge.src_id)
        module_qn = by_path_module.get(src.path) if src else None
        raw = _raw_target(edge.dst_id)
        src_node = node_by_id.get(edge.src_id)
        is_js_ts = src_node is not None and src_node.language in _JS_TS_LANGUAGES
        if is_js_ts:
            hit = _resolve_js_specifier(raw, src.path if src else "", js_ts_modules)
            if hit is not None:
                resolved.append(_bind(edge, hit, CONFIDENCE_SUFFIX))
            continue
        # Python import: try "from M import N" -> by_qn["M.N"], else the dotted
        # path as a module -> by_module; else the head module.
        targets, confidence = _resolve_python_import(raw, by_qn, by_module)
        for target in targets:
            resolved.append(_bind(edge, target, confidence))
            if module_qn is not None:
                bound = raw.split(".")[-1] if "." in raw else raw
                by_local.setdefault((module_qn, bound), []).append(target)
                # import M also binds the head name M.
                head = raw.split(".")[0]
                by_local.setdefault((module_qn, head), []).append(target)

    # Phase B: inherits, then calls/references. Reads by_local, never mutates it.
    for edge in edges:
        if edge.kind not in _SCOPED_KINDS or not _is_unresolved(edge):
            continue
        src = node_by_id.get(edge.src_id)
        module_qn = by_path_module.get(src.path) if src else None
        raw = _raw_target(edge.dst_id)
        is_js_ts = src is not None and src.language in _JS_TS_LANGUAGES
        for dst_id, confidence in _resolve_scoped(
            edge.kind, raw, module_qn, by_local, by_last_segment, is_js_ts
        ):
            resolved.append(_bind(edge, dst_id, confidence))

    # Markdown link pass: bind links_to/embeds within the Markdown domain only.
    # It consults page-only symbol tables and never the code tables above, so a
    # page link never binds to a code node; conversely the code passes never
    # consult these page maps, so domain isolation holds in both directions.
    resolved.extend(_resolve_markdown_links(nodes, edges, node_by_id))

    # Markdown tag pass: bind each page->tag `tagged` edge to its corpus-global
    # tag node by name. Strictly Markdown-domain (tag table built from `tag`
    # nodes only, source must be a markdown node), mirroring the link pass.
    resolved.extend(_resolve_markdown_tags(nodes, edges, node_by_id))

    return resolved


def _is_test_path(path: str) -> bool:
    """Classify a stored path as a test file by naming/location convention.

    True when the basename matches ``test_*``/``*_test`` (any extension) or
    carries a ``.test.``/``.spec.`` infix, or when any directory segment is a
    conventional test directory (``tests``, ``__tests__``, ``spec``, ...).
    """
    pure = PurePosixPath(path)
    segments = pure.parts[:-1]
    if any(seg in _TEST_DIR_SEGMENTS for seg in segments):
        return True
    stem = pure.name
    # Strip all extensions so ``foo.test.ts`` -> ``foo.test`` -> ``foo``.
    base = stem.split(".", 1)[0]
    if base.startswith("test_") or base.endswith("_test"):
        return True
    return ".test." in stem or ".spec." in stem


def enrich_test_edges(nodes: list[Node], resolved: list[Edge]) -> list[Edge]:
    """Derive ``tests`` edges from the resolved graph (no extra parsing).

    A test "tests" the non-test symbols it reaches: for every resolved
    ``calls``/``references``/``imports`` edge whose source is a symbol in a test
    file and whose target is a symbol in a NON-test file, emit one ``tests``
    edge from the test source to that target. The edge inherits the reach
    edge's confidence (the test's knowledge of the target is exactly as certain
    as the bind that connects them). Deduplicated per ``(src, dst)``.

    Pure function over ``(nodes, resolved)`` — deterministic and unit-testable
    in isolation, mirroring :func:`resolve_edges`. Runs after resolution so it
    sees concrete node ids, not raw targets.
    """
    path_by_id = {node.node_id: node.path for node in nodes}
    seen: set[tuple[str, str]] = set()
    test_edges: list[Edge] = []
    for edge in resolved:
        if edge.kind not in _TEST_REACH_KINDS:
            continue
        src_path = path_by_id.get(edge.src_id)
        dst_path = path_by_id.get(edge.dst_id)
        if src_path is None or dst_path is None:
            continue
        if not _is_test_path(src_path) or _is_test_path(dst_path):
            continue
        key = (edge.src_id, edge.dst_id)
        if key in seen:
            continue
        seen.add(key)
        test_edges.append(
            Edge(
                src_id=edge.src_id,
                dst_id=edge.dst_id,
                kind="tests",
                path=src_path,
                resolution=edge.resolution,
                confidence=edge.confidence,
            )
        )
    return test_edges


def _resolve_markdown_tags(
    nodes: list[Node],
    edges: list[Edge],
    node_by_id: dict[str, Node],
) -> list[Edge]:
    """Bind unresolved Markdown ``tagged`` edges to their tag node by name.

    Tag nodes are corpus-global (one ``tag:{name}`` node per distinct tag), so
    the name table maps each tag name to exactly one node id: a ``tagged`` edge
    binds ``exact`` on a single tag node and ``best_effort`` only in the
    defensive case of several nodes sharing a name. A ``tagged`` edge whose raw
    target names no tag node is dropped, mirroring the zero-in-set behaviour.
    """
    by_tag_name: dict[str, list[str]] = {}
    for node in nodes:
        if node.kind == "tag":
            by_tag_name.setdefault(node.name, []).append(node.node_id)

    if not by_tag_name:
        return []

    resolved: list[Edge] = []
    for edge in edges:
        if edge.kind != _MARKDOWN_TAG_KIND or not _is_unresolved(edge):
            continue
        src = node_by_id.get(edge.src_id)
        if src is None or src.language != "markdown":
            continue
        hits = by_tag_name.get(_raw_target(edge.dst_id))
        if not hits:
            continue
        confidence = CONFIDENCE_EXACT if len(hits) == 1 else CONFIDENCE_SUFFIX
        for dst_id in hits:
            resolved.append(_bind(edge, dst_id, confidence))
    return resolved


def _resolve_markdown_links(
    nodes: list[Node],
    edges: list[Edge],
    node_by_id: dict[str, Node],
) -> list[Edge]:
    """Bind unresolved Markdown ``links_to``/``embeds`` edges to page nodes.

    Builds three page-only symbol tables — by stored path, by page title, and by
    slug of the title/stem — from ``page`` nodes, then binds each unresolved
    link/embed edge whose SOURCE is a ``markdown`` node:

    - A relative-path target (contains ``/``, ends in ``.md``/``.markdown``, or
      matches a page after appending those extensions) is normalized against the
      source page's directory (``.``/``..`` resolved POSIX-style, mirroring
      :func:`_resolve_js_specifier`) and matched by stored path -> ``exact``.
    - Otherwise the target is treated as a wikilink title/slug and matched by
      title then by slug -> ``exact`` on a single page, ``best_effort`` on
      several.
    - A pure same-page ``#fragment`` target or an external target (``http(s)``,
      ``mailto:``, a missing file) matches no page and is left DANGLING: the
      edge is dropped, never raised, mirroring the zero-in-set code behaviour.
    """
    by_page_path: dict[str, str] = {}
    by_page_title: dict[str, list[str]] = {}
    by_page_slug: dict[str, list[str]] = {}
    for node in nodes:
        if node.kind != "page":
            continue
        by_page_path[node.path] = node.node_id
        # The page title is reconstructable from stored fields: the page `name`
        # carries the file stem (FEAT-001). A wikilink may name a page by its
        # human title (stem rendered) or by its slug, so key both.
        title = node.name
        by_page_title.setdefault(title, []).append(node.node_id)
        by_page_slug.setdefault(_slug(title), []).append(node.node_id)

    if not by_page_path:
        return []

    resolved: list[Edge] = []
    for edge in edges:
        if edge.kind not in _MARKDOWN_LINK_KINDS or not _is_unresolved(edge):
            continue
        src = node_by_id.get(edge.src_id)
        if src is None or src.language != "markdown":
            continue
        raw = _raw_target(edge.dst_id)
        binding = _resolve_markdown_target(
            raw, src.path, by_page_path, by_page_title, by_page_slug
        )
        for dst_id, confidence in binding:
            resolved.append(_bind(edge, dst_id, confidence))
    return resolved


def _resolve_markdown_target(
    raw: str,
    src_path: str,
    by_page_path: dict[str, str],
    by_page_title: dict[str, list[str]],
    by_page_slug: dict[str, list[str]],
) -> list[tuple[str, float]]:
    """Resolve one Markdown link target to ``(page_node_id, confidence)`` pairs.

    Returns an empty list for a dangling target (external URL, pure fragment,
    missing page), so the caller drops the edge.
    """
    target = raw.strip()
    # Drop a pure same-page fragment (``#anchor``) and any scheme URL / mailto.
    if not target or target.startswith("#"):
        return []
    if "://" in target or target.startswith("mailto:"):
        return []
    # Strip a trailing ``#fragment`` so a link to another page's anchor still
    # binds to that page.
    path_part = target.split("#", 1)[0]

    looks_like_path = (
        "/" in path_part
        or PurePosixPath(path_part).suffix.lower() in _MARKDOWN_EXTENSIONS
    )
    if looks_like_path or path_part:
        hit = _match_page_path(path_part, src_path, by_page_path)
        if hit is not None:
            return [(hit, CONFIDENCE_EXACT)]
        if looks_like_path:
            # A path-shaped target that matches no page is dangling.
            return []

    # Treat the raw target as a wikilink title/slug.
    title_hits = by_page_title.get(path_part)
    if title_hits:
        return _title_bindings(title_hits)
    slug_hits = by_page_slug.get(_slug(path_part))
    if slug_hits:
        return _title_bindings(slug_hits)
    return []


def _title_bindings(hits: list[str]) -> list[tuple[str, float]]:
    confidence = CONFIDENCE_EXACT if len(hits) == 1 else CONFIDENCE_SUFFIX
    return [(nid, confidence) for nid in hits]


def _match_page_path(
    path_part: str, src_path: str, by_page_path: dict[str, str]
) -> str | None:
    """Normalize a relative path target and match it against the page paths.

    Resolves ``.``/``..`` against the source page's directory (POSIX join, same
    normalization as :func:`_resolve_js_specifier`) and tries the normalized
    path as-is and with ``.md``/``.markdown`` appended.
    """
    base_dir = PurePosixPath(src_path).parent
    joined = base_dir / path_part
    parts: list[str] = []
    for part in joined.parts:
        if part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    norm = "/".join(parts)

    candidates = [norm]
    if PurePosixPath(norm).suffix.lower() not in _MARKDOWN_EXTENSIONS:
        candidates.extend(f"{norm}{ext}" for ext in _MARKDOWN_EXTENSIONS)
    for candidate in candidates:
        hit = by_page_path.get(candidate)
        if hit is not None:
            return hit
    return None


def _bind(edge: Edge, dst_id: str, confidence: float) -> Edge:
    return Edge(
        src_id=edge.src_id,
        dst_id=dst_id,
        kind=edge.kind,
        path=edge.path,
        resolution=resolution_of(confidence),
        confidence=confidence,
    )


def _resolve_python_import(
    raw: str, by_qn: dict[str, list[str]], by_module: dict[str, str]
) -> tuple[list[str], float]:
    """Resolve a Python import raw target to in-set node ids and a confidence.

    ``from M import N`` arrives as ``M.N`` and resolves via ``by_qn``; ``import
    M`` arrives as ``M`` and resolves via ``by_module``. A single qualified-name
    or module hit is import-confident (0.95); multiple candidates drop to the
    suffix level (0.55) since the bind is no longer unique; zero hits drop the
    edge.
    """
    qn_hits = by_qn.get(raw)
    if qn_hits:
        return qn_hits, (CONFIDENCE_IMPORT if len(qn_hits) == 1 else CONFIDENCE_SUFFIX)
    module_hit = by_module.get(raw)
    if module_hit is not None:
        return [module_hit], CONFIDENCE_IMPORT
    return [], CONFIDENCE_SUFFIX


def _resolve_scoped(
    kind: str,
    raw: str,
    module_qn: str | None,
    by_local: dict[tuple[str, str], list[str]],
    by_last_segment: dict[str, list[str]],
    is_js_ts: bool,
) -> list[tuple[str, float]]:
    """Resolve an inherits/calls/references edge to ``(dst_id, confidence)``.

    Bare name -> ``by_local[(module, name)]`` (same-module confidence 0.90 on a
    single hit, else the weaker suffix level). Attribute tail / suffix ->
    last-segment match, always suffix confidence (0.55, fix 5). Zero in-set
    nodes -> empty (the edge is dropped). Every JS/TS edge is suffix-level.

    ``by_last_segment`` maps each qualified_name's final dotted segment to the
    node ids (in ``by_qn`` insertion order), so the suffix fallback is an O(1)
    lookup yielding the same ids, in the same order, as the former full scan.
    """
    if module_qn is not None:
        local = by_local.get((module_qn, raw))
        if local:
            if len(local) == 1 and not is_js_ts:
                return [(local[0], CONFIDENCE_SAME_MODULE)]
            return [(nid, CONFIDENCE_SUFFIX) for nid in local]
    # Suffix match on the tail: every qualified_name whose last segment == raw.
    # A bare common name (e.g. ``run``, ``handler``, ``__init__``) can share its
    # last segment with symbols across many unrelated modules; binding to all of
    # them fabricates a near-complete bipartite blow-up of best_effort edges that
    # is noise, not a dependency signal, and does not scale (it quadrupled the
    # edge count on a mid-size repo). When the suffix matches more than
    # ``_SUFFIX_FANOUT_CAP`` distinct in-set symbols, treat it as unresolvable
    # and drop the edge rather than inventing a dense fan-out.
    suffix_hits = by_last_segment.get(raw)
    if suffix_hits and len(suffix_hits) <= _SUFFIX_FANOUT_CAP:
        return [(nid, CONFIDENCE_SUFFIX) for nid in suffix_hits]
    return []


def _resolve_js_specifier(
    specifier: str, importing_path: str, js_ts_modules: dict[str, str]
) -> str | None:
    """Resolve a relative JS/TS import specifier to an in-set module node id.

    Joins the specifier to the importing file's directory, normalizes ``.``/
    ``..`` to a folder-relative POSIX path, then tries candidate stored module
    qualified names in the fixed order: the normalized path exactly (if it
    carries a known JS/TS extension); the normalized path with each extension
    appended; then ``<norm>/index.<ext>`` — first in-set hit wins. A bare
    (non-relative) specifier never resolves.
    """
    if not specifier.startswith("."):
        return None
    base_dir = PurePosixPath(importing_path).parent
    joined = base_dir / specifier
    # Normalize . and .. segments manually against the folder-relative root.
    parts: list[str] = []
    for part in joined.parts:
        if part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    norm = "/".join(parts)

    # Module qualified names are stored without the extension, so each candidate
    # stored path below is reduced to its extension-stripped module qn before
    # the lookup. The order is fixed (NFR 2): exact (known ext), then each
    # appended ext, then <norm>/index.<ext>.
    candidates: list[str] = []
    suffix = PurePosixPath(norm).suffix.lower()
    if suffix in _JS_TS_EXTENSIONS:
        candidates.append(_strip_ext(norm))
    else:
        candidates.extend(_strip_ext(f"{norm}{ext}") for ext in _JS_TS_EXTENSIONS)
        candidates.extend(_strip_ext(f"{norm}/index{ext}") for ext in _JS_TS_EXTENSIONS)

    for candidate in candidates:
        hit = js_ts_modules.get(candidate)
        if hit is not None:
            return hit
    return None


def _strip_ext(path: str) -> str:
    pure = PurePosixPath(path)
    parent = str(pure.parent)
    if parent in (".", ""):
        return pure.stem
    return f"{parent}/{pure.stem}"


def index_folder(
    store: CodeStore,
    folder: str | Path,
    recursive: bool = True,
    *,
    rebuild: bool = False,
    use_default_ignores: bool = True,
) -> IndexReport:
    """Index every source file under ``folder`` into ``store``.

    Incremental-by-default: when the database already holds files and
    ``rebuild`` is ``False``, only changed, added, and deleted files are
    reconciled against the stored etags and the global cross-file resolution is
    re-run (see :func:`_incremental_index`). A full rebuild runs when the
    database is empty OR ``rebuild=True``: the three graph tables and ``files``
    are recreated empty and ``meta`` cleared before the walk, so a full run over
    the same folder yields byte-identical rows. The incremental path leaves the
    database identical to a full rebuild of the same on-disk state.

    ``use_default_ignores`` (default ``True``) controls the built-in directory
    and size exclusions; when ``False`` those are disabled but the
    ``.gitignore`` / ``.duckdbgraphignore`` rules are still honored. The flag is
    threaded identically through the full and incremental paths.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")

    if rebuild or not _db_is_populated(store):
        return _full_index(
            store, folder, recursive, use_default_ignores=use_default_ignores
        )
    return _incremental_index(
        store, folder, recursive, use_default_ignores=use_default_ignores
    )


def _extract_folder(
    folder: Path, recursive: bool, *, use_default_ignores: bool = True
) -> tuple[list[tuple[str, str, str]], list[Node], list[Edge], dict[str, int], int]:
    """Walk ``folder`` and extract every indexable file (no DuckDB writes).

    Returns ``(file_rows, all_nodes, all_edges, languages, skipped)`` where
    ``file_rows`` is ``(rel_path, language, etag)`` per indexed file. Shared by
    the full-build and incremental paths so both see identical extraction.

    The ignore spec (built once per run from the folder's ``.gitignore`` /
    ``.duckdbgraphignore``) and the ``use_default_ignores`` flag prune the walk
    in :func:`_iter_files`; the files that walk dropped are folded into
    ``skipped`` alongside the unsupported-extension and unreadable skips.
    """
    file_rows: list[tuple[str, str, str]] = []
    all_nodes: list[Node] = []
    all_edges: list[Edge] = []
    languages: dict[str, int] = {}

    ignore_scope = _IgnoreScope(folder)
    paths, skipped = _iter_files(
        folder,
        recursive,
        use_default_ignores=use_default_ignores,
        ignore_scope=ignore_scope,
    )

    for abs_path in paths:
        rel = abs_path.relative_to(folder).as_posix()
        language = detect_language(rel)
        if language is None:
            skipped += 1
            continue
        try:
            raw = abs_path.read_bytes()
        except OSError as exc:  # pragma: no cover - rare mid-walk vanish
            skipped += 1
            logger.warning("skipping unreadable file %s: %s", rel, exc)
            continue
        text = raw.decode("utf-8", errors="replace")
        etag = compute_etag(text)
        extractor = resolve_extractor(language)
        extraction = extractor.extract(text, rel)
        all_nodes.extend(extraction.nodes)
        all_edges.extend(extraction.edges)
        file_rows.append((rel, language, etag))
        languages[language] = languages.get(language, 0) + 1

    return file_rows, _dedup_nodes(all_nodes), all_edges, languages, skipped


def _dedup_nodes(nodes: list[Node]) -> list[Node]:
    """Collapse nodes that share a ``node_id`` to the first occurrence.

    Every file-anchored node carries a path+line in its id and so is already
    unique; only the corpus-global ``tag`` node (id ``tag:{name}``, emitted once
    per file that uses the tag) collides across files. Keeping the first
    occurrence yields exactly one stored row per distinct tag while preserving
    the extraction order of all other nodes.
    """
    seen: set[str] = set()
    unique: list[Node] = []
    for node in nodes:
        if node.node_id in seen:
            continue
        seen.add(node.node_id)
        unique.append(node)
    return unique


def _full_index(
    store: CodeStore,
    folder: Path,
    recursive: bool,
    *,
    use_default_ignores: bool = True,
) -> IndexReport:
    """Full rebuild: recreate the tables and rewrite the whole graph."""
    _recreate_tables(store)
    build_stamp = datetime.datetime.now(datetime.UTC)
    file_rows, all_nodes, all_edges, languages, skipped = _extract_folder(
        folder, recursive, use_default_ignores=use_default_ignores
    )
    resolved_edges = resolve_edges(all_nodes, all_edges)
    resolved_edges.extend(enrich_test_edges(all_nodes, resolved_edges))
    _write(store, file_rows, all_nodes, resolved_edges, build_stamp, folder)
    _finalize(store)

    return IndexReport(
        files=len(file_rows) + skipped,
        indexed=len(file_rows),
        skipped=skipped,
        nodes=len(all_nodes),
        edges=len(resolved_edges),
        languages=languages,
        mode="full",
    )


def _db_is_populated(store: CodeStore) -> bool:
    """Return ``True`` when the ``files`` table already holds at least one row."""
    row = store._con.execute("SELECT COUNT(*) FROM files").fetchone()
    return bool(row and row[0] > 0)


def _incremental_index(
    store: CodeStore,
    folder: Path,
    recursive: bool,
    *,
    use_default_ignores: bool = True,
) -> IndexReport:
    """Reconcile a populated database against the current on-disk folder.

    Compares each live file's freshly computed etag against its stored etag to
    classify paths into unchanged / changed / added / deleted. When nothing
    changed, returns a no-op report (``rebuilt=0``, ``deleted=0``) with the true
    whole-graph totals read from the database, without re-extracting anything.
    Otherwise it re-extracts every surviving file (the global
    :func:`resolve_edges` pass needs the unresolved raw targets, which only live
    in a fresh extraction), drops deleted files' rows, resolves globally, and
    rewrites the three tables so the result is identical to a full rebuild of
    the same on-disk state.
    """
    stored = _stored_etags(store)
    file_rows, all_nodes, all_edges, languages, skipped = _extract_folder(
        folder, recursive, use_default_ignores=use_default_ignores
    )
    live = {path: etag for path, _language, etag in file_rows}

    live_paths = set(live)
    stored_paths = set(stored)
    added = live_paths - stored_paths
    deleted = stored_paths - live_paths
    changed = {p for p in live_paths & stored_paths if live[p] != stored[p]}
    rebuilt = added | changed

    if not rebuilt and not deleted:
        return _noop_report(store, skipped, mode="incremental")

    build_stamp = datetime.datetime.now(datetime.UTC)
    resolved_edges = resolve_edges(all_nodes, all_edges)
    resolved_edges.extend(enrich_test_edges(all_nodes, resolved_edges))
    _recreate_tables(store)
    _write(store, file_rows, all_nodes, resolved_edges, build_stamp, folder)
    _finalize(store)

    return IndexReport(
        files=len(file_rows) + skipped,
        indexed=len(file_rows),
        skipped=skipped,
        nodes=len(all_nodes),
        edges=len(resolved_edges),
        languages=languages,
        mode="incremental",
        rebuilt=len(rebuilt),
        deleted=len(deleted),
    )


def _stored_etags(store: CodeStore) -> dict[str, str]:
    """Return the ``{path: etag}`` map of the files currently in the database."""
    rows = store._con.execute("SELECT path, etag FROM files").fetchall()
    return {row[0]: row[1] for row in rows}


def _noop_report(store: CodeStore, skipped: int, *, mode: str) -> IndexReport:
    """Build a no-change report from the whole-graph totals in the database."""
    con = store._con
    file_rows = con.execute("SELECT language FROM files").fetchall()
    languages: dict[str, int] = {}
    for (language,) in file_rows:
        languages[language] = languages.get(language, 0) + 1
    node_row = con.execute("SELECT COUNT(*) FROM nodes").fetchone()
    # Count content edges only: the derived ``member_of`` community edges that
    # the post-index phase adds are analytics artifacts, not extracted content,
    # so the report's edge total matches the full/incremental paths' count of
    # resolved content edges.
    edge_row = con.execute(
        "SELECT COUNT(*) FROM edges WHERE kind != 'member_of'"
    ).fetchone()
    node_count = node_row[0] if node_row else 0
    edge_count = edge_row[0] if edge_row else 0
    return IndexReport(
        files=len(file_rows) + skipped,
        indexed=len(file_rows),
        skipped=skipped,
        nodes=node_count,
        edges=edge_count,
        languages=languages,
        mode=mode,
        rebuilt=0,
        deleted=0,
    )


def _dir_ignore_spec(directory: Path) -> "pathspec.PathSpec | None":
    """Compile the ``.gitignore``/``.duckdbgraphignore`` in ONE directory.

    Reads only ``<directory>/.gitignore`` and ``<directory>/.duckdbgraphignore``
    (each optional). Returns a ``gitignore`` :class:`pathspec.PathSpec` whose
    patterns are anchored to ``directory`` — the caller matches paths relative
    to that directory — or ``None`` when the directory has no ignore patterns.
    ``pathspec`` is imported lazily so a tree with no ignore file pays nothing.
    """
    lines: list[str] = []
    for name in _IGNORE_FILES:
        ignore_file = directory / name
        if not ignore_file.is_file():
            continue
        try:
            lines.extend(ignore_file.read_text(encoding="utf-8").splitlines())
        except OSError as exc:  # pragma: no cover - rare unreadable ignore file
            logger.warning("skipping unreadable ignore file %s: %s", name, exc)

    if not lines:
        return None

    import pathspec

    spec = pathspec.PathSpec.from_lines("gitignore", lines)
    return spec if spec.patterns else None


class _IgnoreScope:
    """Hierarchical gitignore matcher over a tree rooted at ``root``.

    Git's gitignore is hierarchical: a ``.gitignore`` applies to its own
    directory and all descendants, with patterns relative to that file's
    location. This loads each directory's ignore files lazily (once, when first
    reached) and tests a path against every ANCESTOR directory's spec, matching
    the path re-based to each spec's own directory. A single root-only spec (the
    former behaviour) missed nested ``.gitignore`` files entirely — e.g.
    indexing a parent of several repos ignored none of their per-repo rules.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        # abs directory -> compiled spec or None (cached, including the misses)
        self._specs: dict[Path, pathspec.PathSpec | None] = {}

    def _spec_for(self, directory: Path) -> "pathspec.PathSpec | None":
        if directory not in self._specs:
            self._specs[directory] = _dir_ignore_spec(directory)
        return self._specs[directory]

    def _ancestor_dirs(self, directory: Path) -> list[Path]:
        """Return ``directory`` and each ancestor up to (and including) root."""
        dirs = [directory]
        current = directory
        while current != self._root:
            current = current.parent
            dirs.append(current)
            if current == self._root:
                break
        return dirs

    def is_ignored(self, path: Path, *, is_dir: bool) -> bool:
        """Return ``True`` when ``path`` matches any in-scope ancestor spec.

        ``path`` is an absolute path under ``root``. Each ancestor directory's
        spec is tested with ``path`` made relative to THAT directory; a
        directory is tested both bare and with a trailing ``/`` so ``dir`` and
        ``dir/`` patterns both match. The containing directory for an ancestor
        lookup is the path's parent (a file is governed by its directory's and
        all parent directories' ignore files).
        """
        parent = path if is_dir else path.parent
        # Walk from the path's directory up to root, testing each level's spec.
        for ancestor in self._ancestor_dirs(parent):
            spec = self._spec_for(ancestor)
            if spec is None:
                continue
            try:
                rel = path.relative_to(ancestor).as_posix()
            except ValueError:  # pragma: no cover - path not under ancestor
                continue
            if spec.match_file(rel):
                return True
            if is_dir and spec.match_file(f"{rel}/"):
                return True
        return False


def _iter_files(
    folder: Path,
    recursive: bool,
    *,
    use_default_ignores: bool = True,
    ignore_scope: "_IgnoreScope | None" = None,
) -> tuple[list[Path], int]:
    """Walk ``folder`` with directory pruning, returning kept files and a count.

    Uses :func:`os.walk` so excluded directories are pruned in place
    (``dirnames[:] = ...``) and never descended into — the files inside a pruned
    tree are never stat'd, read, or counted. Returns ``(sorted_kept, excluded)``
    where ``excluded`` counts only the files the walk actually reached and
    dropped by the size cap or an ignore-file match (whole pruned trees
    contribute 0).

    When ``use_default_ignores`` is ``True`` (the default), the built-in
    directory set (:data:`_DEFAULT_IGNORED_DIRS`) is pruned and files strictly
    larger than :data:`_DEFAULT_MAX_FILE_BYTES` (1 MB; the bound is exclusive, so
    a file of exactly 1_000_000 bytes is kept) are dropped. When ``False`` those
    two rules are skipped, but ``ignore_scope`` (the hierarchical ``.gitignore``
    / ``.duckdbgraphignore`` matcher) is still honored in both cases — it tests
    each path against every ancestor directory's ignore files, so nested ignore
    files are respected, not only one at ``folder``. When ``recursive`` is
    ``False`` only the top folder's files are returned.
    """
    kept: list[Path] = []
    excluded = 0

    for dirpath, dirnames, filenames in os.walk(folder):
        dir_abs = Path(dirpath)

        pruned: list[str] = []
        for name in dirnames:
            if use_default_ignores and name in _DEFAULT_IGNORED_DIRS:
                continue
            child = dir_abs / name
            if ignore_scope is not None and ignore_scope.is_ignored(child, is_dir=True):
                continue
            pruned.append(name)
        dirnames[:] = pruned

        for name in filenames:
            path = dir_abs / name
            if not path.is_file():
                continue
            if ignore_scope is not None and ignore_scope.is_ignored(path, is_dir=False):
                excluded += 1
                continue
            if use_default_ignores and path.stat().st_size > _DEFAULT_MAX_FILE_BYTES:
                excluded += 1
                continue
            kept.append(path)

        if not recursive:
            dirnames[:] = []

    return sorted(kept), excluded


def _recreate_tables(store: CodeStore) -> None:
    """Drop and recreate the content tables for a full rebuild.

    Deferred-index bulk load: the tables are recreated WITHOUT their secondary
    indexes, which are created once after the bulk insert (see
    :func:`_create_indexes`). Building the B-tree indexes against the finished
    tables is far cheaper than maintaining them across the executemany insert.
    Schema DDL is owned by the store so the indexer's recreation and
    ``_ensure_schema`` agree.
    """
    con = store._con
    for table in ("edges", "nodes", "files", "meta", "vectors"):
        con.execute(f"DROP TABLE IF EXISTS {table}")
    for ddl in SCHEMA_DDL:
        con.execute(ddl)


def _create_indexes(store: CodeStore) -> None:
    """Create the secondary indexes after the bulk load (deferred-index)."""
    con = store._con
    for ddl in INDEX_DDL:
        con.execute(ddl)


# Edge kinds that carry a real dependency signal for centrality and community
# detection. Structural-containment (``defines``) and Markdown-only edges are
# excluded so the ranking reflects code dependencies, not nesting.
_ANALYTICS_EDGE_KINDS = ("imports", "calls", "inherits", "references")

_PAGERANK_DAMPING = 0.85
_PAGERANK_ITERATIONS = 20


def _finalize(store: CodeStore) -> None:
    """Deferred-index creation followed by the index-time post-index phase.

    Creates the secondary indexes against the finished tables, then computes and
    persists the whole-graph analytics the tool layer reads: a PageRank-style
    ``centrality`` per node and ``member_of`` community edges. Both are derived
    from the stored dependency edges, so they always reflect exactly what was
    written.
    """
    _create_indexes(store)
    _compute_centrality(store)
    _compute_communities(store)
    _compute_embeddings(store)


def _analytics_adjacency(store: CodeStore) -> tuple[list[str], dict[str, list[str]]]:
    """Return ``(node_ids, out_edges)`` over the dependency edge kinds.

    ``out_edges`` maps a src node id to the list of dst node ids it points at,
    restricted to edges whose BOTH endpoints are real nodes (a dangling edge is
    skipped). Shared by centrality and community detection so they see one
    consistent graph.
    """
    con = store._con
    node_ids = [r[0] for r in con.execute("SELECT node_id FROM nodes").fetchall()]
    node_set = set(node_ids)
    placeholders = ", ".join(["?"] * len(_ANALYTICS_EDGE_KINDS))
    out_edges: dict[str, list[str]] = {}
    for src, dst in con.execute(
        f"SELECT src_id, dst_id FROM edges WHERE kind IN ({placeholders})",
        list(_ANALYTICS_EDGE_KINDS),
    ).fetchall():
        if src in node_set and dst in node_set:
            out_edges.setdefault(src, []).append(dst)
    return node_ids, out_edges


def _compute_centrality(store: CodeStore) -> None:
    """Compute a PageRank-style centrality per node and persist it.

    Standard iterative PageRank over the dependency subgraph with damping
    ``_PAGERANK_DAMPING``; dangling nodes (no out-edges) redistribute their mass
    uniformly so the vector stays a probability distribution. Converges in a
    fixed, small number of iterations — the graph is a snapshot, not a live
    system, so a fixed iteration budget is enough and keeps the cost bounded.
    """
    node_ids, out_edges = _analytics_adjacency(store)
    n = len(node_ids)
    if n == 0:
        return
    rank = {nid: 1.0 / n for nid in node_ids}
    base = (1.0 - _PAGERANK_DAMPING) / n
    for _ in range(_PAGERANK_ITERATIONS):
        dangling = sum(rank[nid] for nid in node_ids if not out_edges.get(nid))
        shared = _PAGERANK_DAMPING * dangling / n
        nxt = {nid: base + shared for nid in node_ids}
        for src, dsts in out_edges.items():
            share = _PAGERANK_DAMPING * rank[src] / len(dsts)
            for dst in dsts:
                nxt[dst] += share
        rank = nxt
    store._con.executemany(
        "UPDATE nodes SET centrality = ? WHERE node_id = ?",
        [(rank[nid], nid) for nid in node_ids],
    )


def _compute_communities(store: CodeStore) -> None:
    """Assign nodes to communities and persist ``member_of`` edges.

    A frugal community pass: weakly-connected components over the undirected
    dependency subgraph via union-find. This is the single-file,
    zero-dependency stand-in for Louvain modularity — it groups nodes that
    depend on one another into a stable component id, which is what the
    ``analytics`` tool needs for clusters; a finer modularity split can replace
    it later without changing the stored shape (``member_of`` edges).
    """
    node_ids, out_edges = _analytics_adjacency(store)
    if not node_ids:
        return
    parent = {nid: nid for nid in node_ids}

    def find(x: str) -> str:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for src, dsts in out_edges.items():
        for dst in dsts:
            union(src, dst)

    con = store._con
    con.executemany(
        "INSERT INTO edges (src_id, dst_id, kind, path, resolution, confidence) "
        "VALUES (?, ?, 'member_of', '', 'exact', ?)",
        [(nid, find(nid), CONFIDENCE_EXACT) for nid in node_ids],
    )


# Node kinds that carry a searchable identifier — the symbols `search` and
# `find_similar` match against. File/import/structural nodes are not embedded.
_EMBEDDABLE_KINDS = ("module", "class", "function", "page", "heading")


def _embedding_text(kind: str, name: str, qualified_name: str) -> str:
    """The text embedded for a symbol: its kind and identifier, de-dotted.

    Dotted qualified names are split into words so the embedder sees the path
    segments as tokens (``payments.charge.Charge`` -> ``payments charge
    Charge``), which an identifier-level model matches on far better than one
    opaque dotted string.
    """
    words = qualified_name.replace(".", " ").replace("::", " ").replace("/", " ")
    return f"{kind} {name} {words}".strip()


def _compute_embeddings(store: CodeStore) -> None:
    """Embed each searchable symbol and store one vector per node.

    The embedder is resolved from ``settings.embedder`` (``fastembed`` in
    production, ``hashing`` in tests/offline). The embedded text is the symbol's
    kind + identifier (see :func:`_embedding_text`) — the graph stores
    relationships, not source lines, so search is intent-over-identifiers. The
    backend id and dimensions are recorded in ``meta`` so a backend change is
    detectable. A resolver/import failure is logged and skipped rather than
    failing the whole index, so an index still builds without the embedding
    extra available.
    """
    con = store._con
    placeholders = ", ".join(["?"] * len(_EMBEDDABLE_KINDS))
    rows = con.execute(
        f"SELECT node_id, kind, name, qualified_name FROM nodes "
        f"WHERE kind IN ({placeholders})",
        list(_EMBEDDABLE_KINDS),
    ).fetchall()
    if not rows:
        return
    try:
        embedder = resolve_embedder(store.settings.embedder)
        texts = [_embedding_text(kind, name, qn) for _nid, kind, name, qn in rows]
        vectors = embedder.embed(texts)
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the index
        logger.warning("skipping embeddings (%s): %s", store.settings.embedder, exc)
        return
    con.executemany(
        "INSERT INTO vectors (node_id, embedding) VALUES (?, ?)",
        [(row[0], vector) for row, vector in zip(rows, vectors, strict=True)],
    )
    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        ["embedder", embedder.model_identifier],
    )
    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        ["embedding_dimensions", str(embedder.dimensions)],
    )


def _write(
    store: CodeStore,
    file_rows: list[tuple[str, str, str]],
    nodes: list[Node],
    edges: list[Edge],
    build_stamp: datetime.datetime,
    folder: Path,
) -> None:
    """Bulk-insert files, nodes, edges and persist meta.

    FIX 3: every ``files`` row carries ``stamped_at`` set to the build-start
    time. It is WRITTEN but not read in v1 — it is reserved for the incremental
    pass, which will compare a file's stored etag/stamp to decide whether to
    rebuild it. Keep it, it is not dead.
    """
    con = store._con
    # Bulk-insert with executemany rather than a per-row execute loop: at scale
    # (tens of thousands of nodes, hundreds of thousands of edges) one round-trip
    # per row is the dominant cost and does not finish in reasonable time.
    # DuckDB's executemany rejects an empty parameter list, so each insert is
    # guarded for the empty-index case.
    if file_rows:
        con.executemany(
            "INSERT INTO files (path, language, etag, stamped_at) VALUES (?, ?, ?, ?)",
            [(p, lang, etag, build_stamp) for p, lang, etag in file_rows],
        )
    if nodes:
        con.executemany(
            """
            INSERT INTO nodes
                (node_id, kind, name, qualified_name, path,
                 start_line, end_line, language)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    node.node_id,
                    node.kind,
                    node.name,
                    node.qualified_name,
                    node.path,
                    node.start_line,
                    node.end_line,
                    node.language,
                )
                for node in nodes
            ],
        )
    if edges:
        con.executemany(
            "INSERT INTO edges (src_id, dst_id, kind, path, resolution, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (e.src_id, e.dst_id, e.kind, e.path, e.resolution, e.confidence)
                for e in edges
            ],
        )
    con.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        ["schema_version", str(SCHEMA_VERSION)],
    )
    con.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        ["root", str(folder)],
    )
    branch, head_commit = _git_revision(folder)
    if branch is not None:
        con.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ["branch", branch])
    if head_commit is not None:
        con.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ["head_commit", head_commit],
        )


def _git_revision(folder: Path) -> tuple[str | None, str | None]:
    """Return ``(branch, head_commit)`` for ``folder``, or ``(None, None)``.

    Reads ``.git/HEAD`` and the referenced ref file directly — no ``git``
    subprocess — so it works wherever the folder lives and never requires git to
    be installed. A non-git folder, a detached HEAD, or an unreadable ref
    degrades to ``None`` for the missing part rather than raising, so indexing a
    plain directory is unaffected. Walks up from ``folder`` to find the
    enclosing ``.git`` (the indexed folder may be a subdirectory of the repo).
    """
    git_dir = _find_git_dir(folder)
    if git_dir is None:
        return (None, None)
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return (None, None)
    if head.startswith("ref:"):
        ref = head[4:].strip()  # e.g. "refs/heads/main"
        branch = ref.rsplit("/", 1)[-1] if "/" in ref else ref
        commit = _read_git_ref(git_dir, ref)
        return (branch, commit)
    # Detached HEAD: the file holds the commit sha directly; no branch.
    return (None, head or None)


def _find_git_dir(folder: Path) -> Path | None:
    """Return the enclosing ``.git`` directory, walking up, or ``None``."""
    current = folder.resolve()
    for candidate in (current, *current.parents):
        git = candidate / ".git"
        if git.is_dir():
            return git
    return None


def _read_git_ref(git_dir: Path, ref: str) -> str | None:
    """Resolve a ref to its commit sha via the loose ref file or packed-refs."""
    loose = git_dir / ref
    try:
        return loose.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    packed = git_dir / "packed-refs"
    try:
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith(("#", "^")) and line.endswith(ref):
                return line.split(" ", 1)[0]
    except OSError:
        pass
    return None


def _rebuild_file(
    store: CodeStore,
    path: str,
    extraction: Extraction,
    language: str,
    etag: str,
    stamp: datetime.datetime,
) -> None:
    """Delete one file's rows across all three tables and re-insert them.

    This is the file-keyed incremental seam. In v1 it is exercised ONLY by the
    seam test, never by the full-build :func:`index_folder` path. Deleting
    ``WHERE path = ?`` removes exactly that file's contribution; re-inserting
    the extraction's rows rebuilds it, leaving every other file untouched.
    """
    con = store._con
    con.execute("DELETE FROM edges WHERE path = ?", [path])
    con.execute("DELETE FROM nodes WHERE path = ?", [path])
    con.execute("DELETE FROM files WHERE path = ?", [path])
    con.execute(
        "INSERT INTO files (path, language, etag, stamped_at) VALUES (?, ?, ?, ?)",
        [path, language, etag, stamp],
    )
    for node in extraction.nodes:
        con.execute(
            """
            INSERT INTO nodes
                (node_id, kind, name, qualified_name, path,
                 start_line, end_line, language)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                node.node_id,
                node.kind,
                node.name,
                node.qualified_name,
                node.path,
                node.start_line,
                node.end_line,
                node.language,
            ],
        )
    for edge in extraction.edges:
        con.execute(
            "INSERT INTO edges (src_id, dst_id, kind, path, resolution, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                edge.src_id,
                edge.dst_id,
                edge.kind,
                edge.path,
                edge.resolution,
                edge.confidence,
            ],
        )
