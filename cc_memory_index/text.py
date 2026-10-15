"""Single-line text helpers shared by the parser, the renderer and the rescuer."""

from __future__ import annotations

import re

SEPARATOR = " · "
BOM = "\ufeff"

# CR, LF and TAB become spaces (DESIGN §5.1). NEL, LS and PS are treated the same way because
# some frontmatter readers (YAML 1.1, JavaScript's multiline regexes) see them as line breaks.
_TO_SPACE = re.compile("[\r\n\t\u0085\u2028\u2029]")
# Every other C0 and C1 control and DEL, the bidirectional embedding, override and isolate
# controls (which can make text display differently from what it says), and U+FEFF.
_REMOVE = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\ufeff]")
_OPEN = "(["
_CLOSE = ")]"


def strip_bom(text: str) -> str:
    """text without one leading byte order mark: the one a file may start with, never more."""
    return text[len(BOM):] if text.startswith(BOM) else text


def utf8_prefix(text: str, max_bytes: int) -> str:
    """The longest prefix of text that fits in max_bytes of UTF-8."""
    head = text[:max_bytes]
    raw = head.encode("utf-8", "surrogatepass")
    if len(raw) <= max_bytes:
        return head
    return head[: len(raw[:max_bytes].decode("utf-8", "ignore"))]


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
