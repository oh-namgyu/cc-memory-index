"""The Markdown structure that decides whether text is read as text: fenced code blocks, HTML
comments and inline code spans (DESIGN §5.2, §8).

A line inside a fenced code block or an HTML comment is not an index line (migrate) and holds no
link; neither does an inline code span, so `[[x]]` or `[x](x.md)` written as code is not a link.
"""

from __future__ import annotations

import collections
import re

_FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})(.*)\Z")
_COMMENT_OPEN = "<!--"
_COMMENT_CLOSE = "-->"
_TICKS = re.compile(r"`+")


class Verbatim:
    """Fenced code blocks (``` or ~~~, at any indentation) and HTML comments that start a line.

    They hold no index lines: a '- ' or '## ' line inside them is code or commented out. A block
    that is never closed runs to the end of the file, as in CommonMark.
    """

    def __init__(self) -> None:
        self._fence = ""
        self._comment = False

    def inside(self, line: str) -> bool:
        """True when line opens, continues or closes such a block."""
        if self._comment:
            self._comment = _COMMENT_CLOSE not in line
            return True
        m = _FENCE.match(line)
        if self._fence:
            if m and m.group(1)[0] == self._fence[0] and len(m.group(1)) >= len(self._fence) and not m.group(2).strip():
                self._fence = ""
            return True
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            self._fence = m.group(1)
            return True
        rest = line.lstrip(" \t")
        if rest.startswith(_COMMENT_OPEN):
            self._comment = _COMMENT_CLOSE not in rest[len(_COMMENT_OPEN):]
            return self._comment
        return False


def _code_spans(line: str) -> list[tuple[int, int]]:
    """(start, end) of each inline code span, as CommonMark finds them: a run of n backticks opens a
    span that the next run of exactly n backticks closes; a run without such a closer is literal.
    Every run is looked at once, so the time is linear in the length of line."""
    runs = [m.span() for m in _TICKS.finditer(line)]
    later: dict[int, collections.deque[int]] = {}
    for i, (start, end) in enumerate(runs):
        later.setdefault(end - start, collections.deque()).append(i)
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(runs):
        same = later[runs[i][1] - runs[i][0]]
        while same and same[0] <= i:
            same.popleft()
        if same:
            close = same.popleft()
            spans.append((runs[i][0], runs[close][1]))
            i = close
        i += 1
    return spans


def blank_code(line: str) -> str:
    """line with every inline code span, backticks included, replaced by as many spaces: link text
    may still hold code, but nothing inside the code is read as a link."""
    out: list[str] = []
    at = 0
    for start, end in _code_spans(line):
        out += [line[at:start], " " * (end - start)]
        at = end
    return "".join(out) + line[at:]


def prose_lines(text: str) -> list[str]:
    """The lines of text outside fenced code blocks and HTML comments, with their code spans blanked."""
    verbatim = Verbatim()
    return [blank_code(line) for line in text.split("\n") if not verbatim.inside(line)]
