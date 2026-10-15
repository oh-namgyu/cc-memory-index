"""Single frontmatter lines: 'key: value' lines, sequence items and their scalar values (DESIGN §3.1, §3.2).

Only bare scalars, double-quoted strings (\\" and \\\\ escapes) and single-quoted strings ('' escape)
are understood. Everything else returns None and is treated as opaque by the caller.
"""

from __future__ import annotations

import re
from typing import Optional, Union

from .text import sanitize

KEY = re.compile(r"( *)([A-Za-z0-9_][A-Za-z0-9_.-]*):(?:[ \t]+(.*))?\Z")
SEQ = re.compile(r"-(?:[ \t]|\Z)")
_KEY_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_COMMENT = re.compile(r"[ \t]#")
# Flow collections, block scalars, anchors, aliases, tags and reserved indicators.
_OPAQUE_START = "[]{}|>&*!%@`"
_NO_END = -1


def is_null(raw: Optional[str]) -> bool:
    """True for an empty value or one that is only a comment."""
    return raw is None or not raw.strip() or raw.strip().startswith("#")


def value(raw: Optional[str]) -> Optional[str]:
    """Scalar value of the text after 'key:', or None for null and anything opaque."""
    if raw is None or is_null(raw):
        return None
    raw = raw.strip()
    quoted = _quoted(raw)
    if quoted is not None:
        text, end = quoted
        return text if end != _NO_END and _trailer_ok(raw[end:]) else None
    if raw[0] in _OPAQUE_START:
        return None
    cut = _COMMENT.search(raw)
    return raw[: cut.start()].rstrip() if cut else raw


def unclosed(raw: Optional[str]) -> bool:
    """True for a quoted value that does not end on its line: YAML reads it on into the next lines."""
    quoted = None if raw is None or not raw.strip() else _quoted(raw.strip())
    return quoted is not None and quoted[1] == _NO_END


def item_value(line: str) -> str:
    """The value of a sequence item line: after its '- ' markers, and after 'key:' when it has one."""
    rest = line.lstrip(" ")
    while SEQ.match(rest):
        rest = rest[1:].lstrip(" \t")
    m = KEY.match(rest)
    return (m.group(3) or "") if m else rest


def entry_line(key: str, value: Union[str, int]) -> str:
    """'key: value' as the patcher writes it: an int bare, a string sanitised and double-quoted."""
    if not _KEY_NAME.match(key) or key == "metadata":
        raise ValueError(f"invalid frontmatter key {key!r}")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TypeError(f"value for {key!r} must be str or int")
    if isinstance(value, int):
        return f"{key}: {value}"
    return f'{key}: "' + sanitize(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _quoted(raw: str) -> Optional[tuple[str, int]]:
    """(text, index after the closing quote or _NO_END) for a quoted scalar, else None."""
    if raw[0] == '"':
        return _double(raw)
    if raw[0] == "'":
        return _single(raw)
    return None


def _trailer_ok(rest: str) -> bool:
    return not rest.strip() or (rest[:1] in (" ", "\t") and rest.strip().startswith("#"))


def _double(raw: str) -> tuple[str, int]:
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
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return "".join(out), _NO_END


def _single(raw: str) -> tuple[str, int]:
    out: list[str] = []
    i = 1
    while i < len(raw):
        if raw[i] == "'":
            if raw[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(raw[i])
        i += 1
    return "".join(out), _NO_END
