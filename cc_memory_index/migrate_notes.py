"""Text of a hand-written MEMORY.md that is not an index line, kept verbatim in a notes file (DESIGN §8).

Index lines are the top-level '- ' list lines and the '## ' and '### ' headings. Every other line
of the index body (paragraphs, indented list items, fenced code blocks with their fences, tables,
block quotes, HTML) is a note. migrate moves the notes, byte for byte and in order, into one new
memory file, under the headings they appeared under, so that no structure is turned into bullets.
"""

from __future__ import annotations

import itertools
import re
from typing import Callable, Optional, Sequence

NOTES_STEM = "index-notes"
NOTES_TITLE = "# Notes moved from MEMORY.md"
_WHAT = "code blocks, nested lists and paragraphs that were not index lines"
NOTES_DESCRIPTION = f"Text moved out of MEMORY.md by cc-memory-index migrate — {_WHAT}"
_FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})(.*)\Z")
_COMMENT_OPEN = "<!--"
_COMMENT_CLOSE = "-->"


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


class Notes:
    """The notes of an index in order (lines) and the body of the notes file (body).

    In the body each note sits under the '## ' and '### ' lines it appeared under, as written; a
    heading is written only for a part that has notes. Notes that a blank line or an index line
    separated are separated by one blank line.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.body: list[str] = []
        self._heads: list[tuple[bool, str]] = []
        self._gap = False

    def heading(self, line: str, sub: bool) -> None:
        """A '## ' (sub=False) or '### ' heading of the index, written before the next note under it."""
        section = [head for head in self._heads if sub and not head[0]]
        self._heads = section + [(sub, line)]

    def gap(self) -> None:
        self._gap = True

    def add(self, line: str) -> None:
        for _, head in self._heads:
            self.body += ["", head]
        if not self._heads and (self._gap or not self.body):
            self.body.append("")
        self._heads, self._gap = [], False
        self.body.append(line)
        self.lines.append(line)


def notes_name(taken: Callable[[str], bool]) -> str:
    """index-notes.md, else index-notes-2.md, index-notes-3.md ...: the first name not taken."""
    names = (NOTES_STEM + (f"-{n}" if n > 1 else "") + ".md" for n in itertools.count(1))
    return next(name for name in names if not taken(name))


def notes_text(name: str, body: Sequence[str]) -> str:
    """The notes file: frontmatter making it a resident reference memory, a title, then body."""
    idx = f"[Notes moved from MEMORY.md]({name}) — {_WHAT}"
    head = ["---", f"name: {name[: -len('.md')]}", f"description: {NOTES_DESCRIPTION}", "metadata:",
            "  type: reference", "  tier: resident", f'  idx: "{idx}"', '  idx_section: ""', '  idx_sub: ""',
            "  idx_order: 0", "---", NOTES_TITLE]
    return "\n".join(head + list(body)) + "\n"


def first_missing(notes: Sequence[str], text: Optional[str]) -> Optional[str]:
    """The first note that is not among the lines of text in order (text None: the file is absent)."""
    found = iter(() if text is None else text.split("\n"))
    for note in notes:
        if not any(line == note for line in found):
            return note
    return None
