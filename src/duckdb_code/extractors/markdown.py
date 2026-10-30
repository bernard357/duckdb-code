"""The Markdown extractor.

Parses one Markdown file with ``markdown-it-py`` (CommonMark) and emits a single
``page`` node, a ``heading`` node per heading, intra-file ``defines`` edges
(page -> heading, always ``exact``), and *unresolved* cross-file edges:
``links_to`` for links and wikilinks, ``embeds`` for images, and ``tagged`` for
inline ``#tags`` and YAML front-matter tags (each distinct tag also yields a
``tag`` node). An unresolved edge carries its raw target string in ``dst_id``
(prefixed with the shared ``UNRESOLVED`` sentinel) — the indexer binds it
against the cross-file symbol table. The extractor never resolves cross-file: by
contract it sees only its own file.

Wikilinks (``[[title]]``) and ``#tags`` are not CommonMark, so they are scanned
from the raw inline/paragraph text with regexes, skipping code spans and fenced
code blocks.
"""

import re
from pathlib import PurePosixPath

from duckdb_code.extractors.base import Extraction
from duckdb_code.extractors.python import UNRESOLVED
from duckdb_code.models import Edge, Node

_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
_TAG = re.compile(r"(?:(?<=\s)|^)#([A-Za-z][\w/-]*)")
_SLUG_STRIP = re.compile(r"[^\w\- ]+")


def _node_id(kind: str, qualified_name: str, path: str, start_line: int) -> str:
    return f"{kind}:{qualified_name}@{path}:{start_line}"


def _unresolved_target(raw: str) -> str:
    return f"{UNRESOLVED}{raw}"


def _slug(text: str) -> str:
    """Return a GitHub-style slug: lowercase, punctuation stripped, spaces to -."""
    cleaned = _SLUG_STRIP.sub("", text.strip().lower())
    return "-".join(cleaned.split())


class MarkdownExtractor:
    """Extract graph nodes and edges from a single Markdown file."""

    @property
    def language(self) -> str:
        return "markdown"

    def extract(self, text: str, path: str) -> Extraction:
        from markdown_it import MarkdownIt

        tokens = MarkdownIt("commonmark").parse(text)
        builder = _MarkdownBuilder(path, text)
        builder.run(tokens)
        return Extraction(nodes=builder.nodes, edges=builder.edges)


class _MarkdownBuilder:
    """Single pass over the token stream collecting nodes and unresolved edges."""

    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.text = text
        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        # De-dup set for (src_id, raw_target, kind) within the file.
        self._seen_edges: set[tuple[str, str, str]] = set()
        # De-dup slugs for deterministic heading ids.
        self._slug_counts: dict[str, int] = {}
        # Tag node ids already emitted, keyed by node_id.
        self._tag_ids: set[str] = set()
        self._page_id = ""
        self._title = ""
        self._title_from_h1_set = False

    # -- helpers ---------------------------------------------------------

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
        raw = raw_target.strip()
        if not raw:
            return
        key = (src_id, raw, kind)
        if key in self._seen_edges:
            return
        self._seen_edges.add(key)
        self.edges.append(
            Edge(
                src_id=src_id,
                dst_id=_unresolved_target(raw),
                kind=kind,
                path=self.path,
                resolution="best_effort",
            )
        )

    def _unique_slug(self, base: str) -> str:
        count = self._slug_counts.get(base, 0)
        self._slug_counts[base] = count + 1
        if count == 0:
            return base
        return f"{base}-{count}"

    def _emit_tag(self, name: str, line: int) -> None:
        # Tag-node id EXCEPTION: a tag is corpus-global (one node per distinct
        # tag name, not anchored to one file), so it deliberately deviates from
        # the `{kind}:{qualified_name}@{path}:{start_line}` scheme every other
        # node follows. The id is the path-independent `tag:{name}`, the
        # qualified_name is the bare name, and the node carries a synthetic
        # empty path / line 0. Identical tag names therefore collide to one
        # node_id; the indexer de-dups those collisions across files into a
        # single stored row (see _dedup_nodes in indexer.py).
        qn = name
        tag_id = f"tag:{name}"
        if tag_id not in self._tag_ids:
            self._tag_ids.add(tag_id)
            self.nodes.append(
                Node(
                    node_id=tag_id,
                    kind="tag",
                    name=name,
                    qualified_name=qn,
                    path="",
                    start_line=0,
                    end_line=0,
                    language="markdown",
                )
            )
        self._add_unresolved(self._page_id, name, "tagged")

    # -- traversal -------------------------------------------------------

    def run(self, tokens: list) -> None:
        total_lines = self.text.count("\n") + 1 if self.text else 1
        stem = PurePosixPath(self.path).stem
        self._page_id = _node_id("page", self.path, self.path, 1)
        self.nodes.append(
            Node(
                node_id=self._page_id,
                kind="page",
                name=stem or PurePosixPath(self.path).name,
                qualified_name=self.path,
                path=self.path,
                start_line=1,
                end_line=total_lines,
                language="markdown",
            )
        )
        self._title = stem

        self._parse_front_matter()

        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token.type == "heading_open":
                inline = tokens[i + 1] if i + 1 < len(tokens) else None
                self._handle_heading(token, inline)
            elif token.type == "inline":
                self._handle_inline(token)
            i += 1

    def _handle_heading(self, open_token, inline) -> None:
        text = inline.content if inline is not None else ""
        slug = self._unique_slug(_slug(text) or "section")
        start, end = self._token_lines(open_token)
        if open_token.tag == "h1" and not self._title_from_h1_set:
            self._title = text
            self._title_from_h1_set = True
        qn = f"{self.path}#{slug}"
        heading_id = _node_id("heading", qn, self.path, start)
        self.nodes.append(
            Node(
                node_id=heading_id,
                kind="heading",
                name=text,
                qualified_name=qn,
                path=self.path,
                start_line=start,
                end_line=end,
                language="markdown",
            )
        )
        self._add_defines(self._page_id, heading_id)

    def _handle_inline(self, token) -> None:
        start, _ = self._token_lines(token)
        for child in token.children or []:
            if child.type == "link_open":
                href = child.attrs.get("href")
                if href:
                    self._add_unresolved(self._page_id, str(href), "links_to")
            elif child.type == "image":
                src = child.attrs.get("src")
                if src:
                    self._add_unresolved(self._page_id, str(src), "embeds")
        # Wikilinks and tags are not CommonMark: scan the raw text, skipping code
        # spans (code_inline children) and fenced/indented code blocks.
        if token.type == "inline":
            for child in token.children or []:
                if child.type in ("code_inline", "fence", "code_block"):
                    continue
                if child.type == "text":
                    self._scan_text(child.content, start)

    def _scan_text(self, text: str, line: int) -> None:
        for match in _WIKILINK.finditer(text):
            self._add_unresolved(self._page_id, match.group(1), "links_to")
        for match in _TAG.finditer(text):
            self._emit_tag(match.group(1), line)

    def _token_lines(self, token) -> tuple[int, int]:
        if token.map is not None:
            return token.map[0] + 1, token.map[1]
        return 1, 1

    # -- front-matter ----------------------------------------------------

    def _parse_front_matter(self) -> None:
        if not self.text.startswith("---"):
            return
        lines = self.text.splitlines()
        if not lines or lines[0].strip() != "---":
            return
        end = None
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                end = idx
                break
        if end is None:
            return
        block = "\n".join(lines[1:end])
        try:
            import yaml

            data = yaml.safe_load(block)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        raw_tags = data.get("tags", data.get("tag"))
        names: list[str] = []
        if isinstance(raw_tags, str):
            names = [raw_tags]
        elif isinstance(raw_tags, (list, tuple)):
            names = [str(item) for item in raw_tags]
        for name in names:
            name = name.strip().lstrip("#")
            if name:
                self._emit_tag(name, 1)
