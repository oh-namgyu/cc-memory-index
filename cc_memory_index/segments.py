"""Reading generated index text back as lines and segments, shared by the renderer, the rescuer and
migrate (DESIGN §4, §6, §8).

A generated list line holds exactly one item. Lines written by hand may hold several: a segment is
one ` · `-separated piece of a list line, split outside brackets and parentheses, or any other
non-blank line of a file's body. Lines and segments are compared whitespace-normalised.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from .text import normalize_ws, sanitize, split_segments

BULLET = "- "
OVERFLOW = "Overflow"
UNRESOLVED = "Unresolved"
_RESERVED = frozenset({OVERFLOW.lower(), UNRESOLVED.lower()})
SECTION_SUFFIX = " (section)"


@dataclass(frozen=True)
class BodySegment:
    """A segment of a file's body and the section and subsection it appeared under ('' before any heading)."""

    text: str
    section: str
    sub: str


def norm(s: str) -> str:
    """Sanitised and whitespace-normalised: the form in which segments are compared."""
    return normalize_ws(sanitize(s))


def pieces(text: str) -> list[str]:
    """The normalised segments that a list line holding text splits into; text that is equal when
    normalised splits into the same pieces."""
    return split_segments(norm(text))


def list_items(lines: Iterable[str]) -> list[str]:
    """The normalised text after '- ' of every list line among lines: in a rendered L0 or L1, its items."""
    return [norm(line[len(BULLET):]) for line in lines if line.startswith(BULLET)]


def item_pieces(lines: Iterable[str]) -> list[str]:
    """The pieces (see pieces) of the list lines among lines: what rendered L0 and L1 text is cut into
    to be compared with other text (DESIGN §4, §6, §8)."""
    return [p for text in list_items(lines) for p in split_segments(text)]


def _reserved(name: str) -> bool:
    base = name
    while base.endswith(SECTION_SUFFIX):
        base = base[: -len(SECTION_SUFFIX)]
    return base.lower() in _RESERVED


def l1_heading(section: str) -> str:
    """The L1 heading of a section; one that could be read as Overflow or Unresolved gets a suffix."""
    return section + SECTION_SUFFIX if _reserved(section) else section


def l1_section(heading: str) -> Optional[str]:
    """Inverse of l1_heading; None for L1's own Overflow and Unresolved headings."""
    if heading.lower() in _RESERVED:
        return None
    if heading.endswith(SECTION_SUFFIX) and _reserved(heading):
        return heading[: -len(SECTION_SUFFIX)]
    return heading


@dataclass(frozen=True)
class BodyLine:
    """A non-blank line of a file's body, stripped: for a list line (listed) the text after '- '."""

    text: str
    section: str
    sub: str
    listed: bool

    def segments(self) -> list[BodySegment]:
        """A list line split on ' · ' outside brackets and parentheses; any other line whole."""
        texts = split_segments(self.text) if self.listed else [self.text]
        return [BodySegment(text, self.section, self.sub) for text in texts]


def body_lines(lines: Iterable[str], l1: bool) -> list[BodyLine]:
    """The lines after a file's head, each with the section and subsection it appeared under.

    '## ' and '### ' headings set the section and subsection (in L1 through l1_section; lines
    under its Overflow and Unresolved headings get no section); they are not returned themselves.
    """
    section, sub = "", ""
    out: list[BodyLine] = []
    for line in lines:
        s = line.strip()
        if s.startswith("### "):
            sub = sanitize(s[4:])
        elif s.startswith("## "):
            name = sanitize(s[3:])
            section, sub = (l1_section(name) or "") if l1 else name, ""
        elif s == "-" or s.startswith("- "):
            out.append(BodyLine(s[1:].strip(), section, sub, True))
        elif s:
            out.append(BodyLine(s, section, sub, False))
    return out


def _opens_body(line: str) -> bool:
    s = line.strip()
    return s.startswith("## ") or s.startswith("### ") or s == "-" or s.startswith("- ")


def split_head(
    lines: Sequence[str], head: Sequence[str], generated: Callable[[str], bool]
) -> tuple[Optional[list[str]], str]:
    """(the body lines, '') when the text starts with head, else (None, the first line that differs).

    Blank lines and the lines accepted by generated (generated lines, which may also be missing)
    are dropped first. What remains must start with head, compared normalised, and continue with
    a heading or a list line: other text there is part of an edited head.
    """
    kept = [line for line in lines if norm(line) and not generated(line)]
    expected = [n for n in map(norm, head) if n]
    for got, want in zip(kept, expected):
        if norm(got) != want:
            return None, norm(got)
    if len(kept) < len(expected):
        return None, ""
    rest = kept[len(expected):]
    if rest and not _opens_body(rest[0]):
        return None, norm(rest[0])
    return rest, ""
