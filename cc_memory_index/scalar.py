"""Scalar values of single 'key: value' frontmatter lines (DESIGN §3.1).

Only bare scalars, double-quoted strings (\\" and \\\\ escapes) and single-quoted strings ('' escape)
are understood. Everything else returns None and is treated as opaque by the caller.
"""

from __future__ import annotations

import re
from typing import Optional

_COMMENT = re.compile(r"[ \t]#")
# Flow collections, block scalars, anchors, aliases, tags and reserved indicators.
_OPAQUE_START = "[]{}|>&*!%@`"


def is_null(raw: Optional[str]) -> bool:
    """True for an empty value or one that is only a comment."""
    return raw is None or not raw.strip() or raw.strip().startswith("#")


def value(raw: Optional[str]) -> Optional[str]:
    """Scalar value of the text after 'key:', or None for null and anything opaque."""
    if raw is None or is_null(raw):
        return None
    raw = raw.strip()
    if raw[0] == '"':
        return _double(raw)
    if raw[0] == "'":
        return _single(raw)
    if raw[0] in _OPAQUE_START:
        return None
    cut = _COMMENT.search(raw)
    return raw[: cut.start()].rstrip() if cut else raw


def _trailer_ok(rest: str) -> bool:
    return not rest.strip() or (rest[:1] in (" ", "\t") and rest.strip().startswith("#"))


def _double(raw: str) -> Optional[str]:
    out: list[str] = []
    i = 1
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append(nxt if nxt in '"\\' else ch + nxt)
            i += 2
            continue
        if ch == '"':
            return "".join(out) if _trailer_ok(raw[i + 1:]) else None
        out.append(ch)
        i += 1
    return None


def _single(raw: str) -> Optional[str]:
    out: list[str] = []
    i = 1
    while i < len(raw):
        if raw[i] == "'":
            if raw[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out) if _trailer_ok(raw[i + 1:]) else None
        out.append(raw[i])
        i += 1
    return None
