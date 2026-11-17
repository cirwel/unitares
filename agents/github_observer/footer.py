"""The lines of a pull request body that a reader sees as the author's own.

Two parsers do the work, and neither is hand-written Markdown:

1. ``markdown-it-py`` tokenises the body as CommonMark, with GitHub's table
   and strikethrough extensions, and renders it to HTML. Raw HTML passes
   through unchanged, as GitHub's renderer passes it. GFM's tagfilter is then
   applied: GitHub shows ``<script>``, ``<textarea>`` and the other filtered
   tags as literal text, so they open nothing.
2. The standard library's HTML parser walks that HTML. Since Python 3.14 it
   reads comments as a browser does, so a ``<!--`` that is never closed hides
   everything after it.

A visible line is a line of a ``<p>`` paragraph that is not inside a
``<blockquote>``, ``<details>``, ``<pre>`` or ``<code>`` element, and that
holds nothing but text. Any other tag, a comment, or an element still open
from an earlier line of the paragraph makes the line not a plain line of
text. GitHub renders each newline inside a paragraph as a line break, so a
line here is a line of the source paragraph, also split at ``<br>``.

``markdown-it-py`` is pure Python and ships in the ``observer`` extra, so the
core install does not need it. Without it this module refuses to read a body
rather than guessing.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

try:
    from markdown_it import MarkdownIt
except ImportError:  # pragma: no cover - exercised by the missing-extra test
    MarkdownIt = None  # type: ignore[assignment,misc]

INSTALL_HINT = "the GitHub observer needs markdown-it-py: pip install -e '.[observer]'"

# Elements whose contents a reader does not see as the author's own line.
_HIDING = frozenset({"blockquote", "details", "pre", "code"})
# Void elements: they never have contents, so they never stay open.
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
# GFM's tagfilter extension (https://github.github.com/gfm/#disallowed-raw-html-extension-).
_TAGFILTER_RE = re.compile(
    r"<(?=/?(?:title|textarea|style|xmp|iframe|noembed|noframes|script|plaintext)(?:[\s/>]|$))",
    re.IGNORECASE,
)


class MissingTokenizer(RuntimeError):
    """``markdown-it-py`` is not installed."""


def tokenizer_available() -> bool:
    return MarkdownIt is not None


def _renderer() -> MarkdownIt:
    if MarkdownIt is None:
        raise MissingTokenizer(INSTALL_HINT)
    return MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])


class _VisibleLines(HTMLParser):
    """Collects visible paragraph lines while keeping a stack of open elements.

    An end tag closes its element and every element opened inside it, as a
    browser does, so ``</blockquote>`` also closes a ``<pre>`` left open in
    the quote. An end tag with no open element of its name is ignored.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._stack: list[str] = []  # open non-void elements other than ``p``
        self._in_p = False
        self._p_base = 0  # stack depth when the current paragraph opened
        self._line: list[str] = []
        self._tainted = False

    def _hidden(self) -> bool:
        return any(tag in _HIDING for tag in self._stack)

    def _end_line(self) -> None:
        if self._in_p and not self._tainted:
            self.lines.append("".join(self._line))
        self._line, self._tainted = [], False

    def _end_paragraph(self) -> None:
        self._end_line()
        self._in_p = False

    def _taint(self) -> None:
        if self._in_p:
            self._tainted = True

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "p":
            self._end_paragraph()
            self._in_p = not self._hidden()
            self._p_base = len(self._stack)
        elif tag == "br":
            self._end_line()
        elif tag in _VOID:
            self._taint()
        else:
            self._stack.append(tag)
            self._taint()

    def handle_startendtag(self, tag: str, attrs) -> None:
        # HTML ignores the self-closing slash on a non-void element:
        # ``<details/>`` opens a details element.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p":
            self._end_paragraph()
        elif tag == "br":  # a browser reads ``</br>`` as ``<br>``
            self._end_line()
        elif tag in self._stack:
            self._taint()
            del self._stack[len(self._stack) - 1 - self._stack[::-1].index(tag) :]
            if self._in_p and len(self._stack) < self._p_base:
                self._end_paragraph()  # the paragraph's container closed
        else:
            self._taint()

    def handle_data(self, data: str) -> None:
        for index, part in enumerate(data.split("\n")):
            if index:
                self._end_line()
            if not part or not self._in_p:
                continue
            if len(self._stack) > self._p_base:  # inside an element in the paragraph
                self._tainted = True
            else:
                self._line.append(part)

    def handle_comment(self, data: str) -> None:
        self._taint()

    def handle_decl(self, decl: str) -> None:
        self._taint()

    def handle_pi(self, data: str) -> None:
        self._taint()

    def unknown_decl(self, data: str) -> None:
        self._taint()

    def close(self) -> None:
        super().close()
        self._end_paragraph()


def visible_paragraph_lines(body: str) -> list[str]:
    """Each visible line of the rendered body, in order, without the spaces
    and tabs around it. Raises ``MissingTokenizer`` without ``markdown-it-py``."""
    html = _TAGFILTER_RE.sub("&lt;", _renderer().render(body))
    parser = _VisibleLines()
    parser.feed(html)
    parser.close()
    return [line.strip(" \t") for line in parser.lines]
