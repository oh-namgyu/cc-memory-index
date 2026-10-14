"""Single-line text helpers shared by the parser, the renderer and the rescuer."""

from __future__ import annotations

import re

SEPARATOR = " · "

# CR, LF and TAB become spaces (DESIGN §5.1). NEL, LS and PS are treated the same way because
# some frontmatter readers (YAML 1.1, JavaScript's multiline regexes) see them as line breaks.
_TO_SPACE = re.compile("[\r\n\t\u0085  ]")
_REMOVE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_OPEN = "(["
_CLOSE = ")]"


def sanitize(s: str) -> str:
    """Reduce untrusted index text to one stripped line without control characters."""
    return _REMOVE.sub("", _TO_SPACE.sub(" ", s)).strip()


def normalize_ws(s: str) -> str:
    """Collapse every run of whitespace to one space and strip."""
    return " ".join(s.split())


def split_segments(line: str) -> list[str]:
    """Split a list line on ' · ' outside brackets and parentheses; drop empty pieces."""
    pieces: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(line):
        ch = line[i]
        if depth == 0 and line.startswith(SEPARATOR, i):
            pieces.append(line[start:i])
            i += len(SEPARATOR)
            start = i
            continue
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        i += 1
    pieces.append(line[start:])
    return [p.strip() for p in pieces if p.strip()]
