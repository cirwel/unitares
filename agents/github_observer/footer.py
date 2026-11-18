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
code, a link, struck-through or other formatted text, it does not. Where
GitHub's HTML differs from a naive reading of the tags, the walker follows
GitHub: an inline element left open closes with its paragraph, a paragraph
inside a table but outside its cells moves in front of the table, a table
part outside any table is ignored, and an end tag does not reach past a
table to close an element opened outside it. Every case is pinned against
GitHub's own rendering in ``tests/fixtures/github_observer/rendered_bodies.json``.

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
# Text and a paragraph may sit in these parts of a table; elsewhere in a table
# a paragraph is moved out in front of it (HTML's foster parenting).
_TABLE_CELLS = frozenset({"td", "th", "caption"})
# Inline elements GitHub's output closes at the end of the paragraph that
# opened them, so an unclosed ``<del>`` does not strike the next paragraph.
_PHRASING = frozenset(
    {
        "a", "abbr", "b", "big", "cite", "code", "del", "dfn", "em", "font", "i", "ins",
        "kbd", "mark", "q", "s", "samp", "small", "span", "strike", "strong", "sub", "sup",
        "time", "tt", "u", "var",
    }
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
    return MarkdownIt("commonmark", {"html": True, "breaks": True, "strikethrough_single_tilde": True}).enable(
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
        self._in_p = False  # inside a paragraph that can hold a footer
        self._p_open = False  # inside any paragraph
        self._p_base = 0  # stack depth when the current paragraph opened
        self._line: list[str] = []
        self._tainted = False

    def _end_line(self) -> None:
        if self._in_p and not self._tainted:
            self.lines.append("".join(self._line))
        self._line, self._tainted = [], False

    def _end_paragraph(self) -> None:
        self._end_line()
        self._in_p = self._p_open = False

    def _close_paragraph(self) -> None:
        """``</p>``: the paragraph ends, and inline elements opened in it close.
        A block element opened in it (``<details>``) stays open."""
        was_open = self._p_open
        self._end_paragraph()
        if was_open and all(tag in _PHRASING for tag in self._stack[self._p_base :]):
            del self._stack[self._p_base :]

    def _placement(self) -> list[str]:
        """The open elements a paragraph opened now would sit inside: inside a
        table but outside its cells, the paragraph goes in front of the table."""
        if "table" in self._stack:
            index = len(self._stack) - 1 - self._stack[::-1].index("table")
            if not any(tag in _TABLE_CELLS for tag in self._stack[index + 1 :]):
                return self._stack[:index]
        return self._stack

    def _taint(self) -> None:
        if self._in_p:
            self._tainted = True

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "p":
            self._close_paragraph()
            self._in_p = all(open_tag in _CONTAINERS for open_tag in self._placement())
            self._p_open = True
            self._p_base = len(self._stack)
        elif tag == "br":
            self._end_line()
        elif tag in _VOID:
            self._taint()
        elif tag in _TABLE_PARTS and tag != "table" and "table" not in self._stack:
            self._taint()  # a table part outside any table is ignored
        else:
            self._stack.append(tag)
            self._taint()

    def handle_startendtag(self, tag: str, attrs) -> None:
        # HTML ignores the self-closing slash on a non-void element:
        # ``<details/>`` opens a details element.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p":
            self._close_paragraph()
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

    # GitHub strips comments, processing instructions and doctype-style
    # declarations; they hide what they enclose and add nothing visible.
    def handle_comment(self, data: str) -> None:
        pass

    def handle_decl(self, decl: str) -> None:
        pass

    def handle_pi(self, data: str) -> None:
        pass

    def unknown_decl(self, data: str) -> None:
        # ``<![CDATA[...]]>``: this parser reads it to ``]]>``, but HTML ends
        # it at the first ``>`` and shows the rest as text. Not a plain line.
        self._taint()

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
    # Whitespace at a line's edges hides nothing, so any kind is stripped
    # there (markdown-it trims a paragraph's trailing U+2028; GitHub keeps
    # it). Inside a line it stays and stops a footer from matching.
    return [line.strip() for line in parser.lines]
