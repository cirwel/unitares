"""The lines of a pull request body that a reader sees as the author's own.

Two parsers do the work, and neither is hand-written Markdown:

1. ``markdown-it-py`` tokenises the body as CommonMark, with GitHub's table
   and strikethrough extensions, and renders it to HTML. As on GitHub, a
   newline inside a paragraph renders as a line break (``<br>``), and raw HTML
   passes through unchanged. GFM's tagfilter is then applied: GitHub shows
   ``<script>``, ``<textarea>`` and the other filtered tags as literal text,
   so they open nothing.
2. The standard library's HTML parser walks that HTML. Since Python 3.14 it
   reads comments as a browser does, so a ``<!--`` that is never closed hides
   everything after it.

A visible line is a stretch of a ``<p>`` paragraph between line breaks that
holds nothing but text. Comments do not count against it: GitHub strips them.
Any other tag on the line, or an element still open from earlier in the
paragraph, makes it not a plain line of text. A newline that is not a line
break (``&#10;``, a newline inside raw HTML) renders as a space and is kept
inside the line, where it stops a footer from matching.

The paragraph itself counts only when every element around it is a plain
container (``<div>`` or a list). Inside a quote, ``<details>``, ``<pre>``,
code, a link, struck-through or other formatted text, it does not.

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

# Elements a footer paragraph may sit inside. Anything else around it (a quote,
# ``<details>``, ``<pre>``, code, a link, ``<del>``, ``<b>`` ...) changes how
# a reader sees it, so the paragraph does not count.
_CONTAINERS = frozenset({"div", "ul", "ol", "li", "dl", "dt", "dd"})
# Void elements: they never have contents, so they never stay open.
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
# An end tag does not reach past these to close an element opened outside
# them (HTML's "has an element in scope"), e.g. ``</blockquote>`` inside a
# ``<table>`` opened in the quote leaves the quote open.
_SCOPE_BARRIERS = frozenset(
    {"applet", "caption", "html", "marquee", "object", "table", "td", "template", "th"}
)
_TABLE_PARTS = frozenset({"table", "caption", "tbody", "thead", "tfoot", "tr", "td", "th"})
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
    return MarkdownIt("commonmark", {"html": True, "breaks": True}).enable(
        ["table", "strikethrough"]
    )


class _VisibleLines(HTMLParser):
    """Collects visible paragraph lines while keeping a stack of open elements.

    An end tag closes its element and every element opened inside it, as a
    browser does, so ``</blockquote>`` also closes a ``<pre>`` left open in
    the quote, unless a scope barrier such as ``<table>`` stands between them.
    An end tag with no open element of its name is ignored.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._stack: list[str] = []  # open non-void elements other than ``p``
        self._in_p = False
        self._p_base = 0  # stack depth when the current paragraph opened
        self._line: list[str] = []
        self._tainted = False

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
            self._in_p = all(open_tag in _CONTAINERS for open_tag in self._stack)
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
            return
        if tag == "br":  # a browser reads ``</br>`` as ``<br>``
            self._end_line()
            return
        self._taint()
        if tag not in self._stack:
            return
        index = len(self._stack) - 1 - self._stack[::-1].index(tag)
        if tag not in _TABLE_PARTS and any(t in _SCOPE_BARRIERS for t in self._stack[index + 1 :]):
            return  # out of scope: the browser ignores this end tag
        del self._stack[index:]
        if self._in_p and len(self._stack) < self._p_base:
            self._end_paragraph()  # the paragraph's container closed

    def handle_data(self, data: str) -> None:
        if not self._in_p or not data:
            return
        if len(self._stack) > self._p_base:  # inside an element in the paragraph
            self._tainted = True
        else:
            self._line.append(data)

    # GitHub strips comments, processing instructions and declarations from
    # the rendered body; they hide what they enclose and add nothing visible.
    def handle_comment(self, data: str) -> None:
        pass

    def handle_decl(self, decl: str) -> None:
        pass

    def handle_pi(self, data: str) -> None:
        pass

    def unknown_decl(self, data: str) -> None:
        pass

    def close(self) -> None:
        super().close()
        self._end_paragraph()


def visible_paragraph_lines(body: str) -> list[str]:
    """Each visible line of the rendered body, in order, without the HTML
    whitespace around it. Raises ``MissingTokenizer`` without ``markdown-it-py``."""
    return lines_of_html(_TAGFILTER_RE.sub("&lt;", _renderer().render(body)))


def lines_of_html(html: str) -> list[str]:
    """The visible lines of already rendered HTML (such as GitHub's own)."""
    parser = _VisibleLines()
    parser.feed(html)
    parser.close()
    # ASCII whitespace only: HTML collapses it at a line's edges, but U+2028
    # and its kind are text, not whitespace, to a browser.
    return [line.strip(" \t\n\r\f") for line in parser.lines]
