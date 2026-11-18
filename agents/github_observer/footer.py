"""The lines of a pull request body that a reader sees as the author's own,
read from GitHub's own rendering of the body (``body_html``).

GitHub renders the Markdown, resolves the HTML (implied end tags, misnested
formatting, tables), sanitizes it, and serializes the resulting tree. So
``body_html`` is balanced: every element GitHub keeps is explicitly closed,
and comments are gone. Reading it needs no Markdown or HTML tree-building
rules of our own, only a walk over a tree GitHub has already built.

That walk is strict. Input that is not balanced in this way, or that carries
a comment, declaration or processing instruction, did not come from GitHub's
renderer, and is refused (``UnrenderedHTML``) rather than guessed at.

A visible line is a stretch of a ``<p>`` paragraph between ``<br>`` breaks
that holds nothing but text directly inside the paragraph. Any element on
the line (a link, code, an image, a task-list checkbox) makes it not a plain
line of text. The paragraph counts only when every element around it is a
plain container (``<div>`` or a list): inside a quote, ``<details>``,
``<pre>``, a table, or formatted text, it does not.
"""

from __future__ import annotations

from html.parser import HTMLParser

# Elements a footer paragraph may sit inside. Anything else around it (a
# quote, ``<details>``, ``<pre>``, a table, ``<del>``, ``<b>`` ...) changes how
# a reader sees it, so the paragraph does not count.
_CONTAINERS = frozenset({"div", "ul", "ol", "li", "dl", "dt", "dd"})
# Void elements: they have no contents and no end tag.
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)


class UnrenderedHTML(ValueError):
    """The HTML is not the balanced output of GitHub's renderer."""


class _VisibleLines(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._stack: list[str] = []  # open elements, ``p`` included
        self._p_depth = -1  # stack index of the open paragraph that counts, or -1
        self._line: list[str] = []
        self._tainted = False

    def _in_counted_p(self) -> bool:
        return self._p_depth >= 0

    def _end_line(self) -> None:
        if self._in_counted_p() and not self._tainted:
            self.lines.append("".join(self._line))
        self._line, self._tainted = [], False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "br":
            self._end_line()
            return
        if self._in_counted_p():
            self._tainted = True  # an element on the line
        if tag in _VOID:
            return
        if tag == "p" and all(open_tag in _CONTAINERS for open_tag in self._stack):
            self._p_depth = len(self._stack)
            self._line, self._tainted = [], False
        self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag not in _VOID:
            raise UnrenderedHTML(f"self-closing <{tag}/>")
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            raise UnrenderedHTML(f"end tag for void <{tag}>")
        if not self._stack or self._stack[-1] != tag:
            raise UnrenderedHTML(f"</{tag}> does not close the open element")
        self._stack.pop()
        if self._in_counted_p():
            if len(self._stack) == self._p_depth:  # the paragraph itself closed
                self._end_line()
                self._p_depth = -1
            else:
                self._tainted = True  # an element on the line

    def handle_data(self, data: str) -> None:
        if not self._in_counted_p() or not data:
            return
        if len(self._stack) > self._p_depth + 1:  # inside an element in the paragraph
            self._tainted = True
        else:
            self._line.append(data)

    def handle_comment(self, data: str) -> None:
        raise UnrenderedHTML("comment")

    def handle_decl(self, decl: str) -> None:
        raise UnrenderedHTML("declaration")

    def handle_pi(self, data: str) -> None:
        raise UnrenderedHTML("processing instruction")

    def unknown_decl(self, data: str) -> None:
        raise UnrenderedHTML("declaration")

    def close(self) -> None:
        super().close()
        if self._stack:
            raise UnrenderedHTML(f"unclosed <{self._stack[-1]}>")


def visible_paragraph_lines(body_html: str) -> list[str]:
    """Each visible line of GitHub's rendered body, in order, without the
    whitespace around it. Raises ``UnrenderedHTML`` for HTML that is not
    GitHub's balanced output."""
    parser = _VisibleLines()
    parser.feed(body_html)
    parser.close()
    # Whitespace at a line's edges hides nothing, so any kind is stripped
    # there. Inside a line it stays and stops a footer from matching.
    return [line.strip() for line in parser.lines]
