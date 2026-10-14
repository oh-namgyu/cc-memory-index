"""Strict-subset frontmatter parser and insert-only patcher (DESIGN §3.1, §3.2).

Nothing here evaluates input: lines are classified with regular expressions and every line
the parser does not understand is left byte-for-byte as it was.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping, Optional, Union

from . import scalar
from .text import sanitize

WINDOW_BYTES = 64 * 1024
_BOM = "﻿"
_FENCE = "---"
_KEY = re.compile(r"( *)([A-Za-z0-9_][A-Za-z0-9_.-]*):(?:[ \t]+(.*))?\Z")
_KEY_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_ABSENT = -1
_UNCLOSED = -2
_UNCLOSED_MSG = "no closing '---' line within the first 64 KiB"
_REPLACEABLE = frozenset({"tier"})
_INDENT = 2


class PatchError(Exception):
    """The frontmatter cannot be patched without risking its structure."""


@dataclass
class Frontmatter:
    top: dict[str, str]
    metadata: dict[str, str]
    present: bool
    ok: bool
    error: Optional[str]

    def get(self, key: str) -> Optional[str]:
        """Value from the metadata block, else from the top level, else None."""
        if key in self.metadata:
            return self.metadata[key]
        return self.top.get(key)


@dataclass
class _Entry:
    scope: str
    key: str
    line: int
    end: int
    indent: int
    recorded: bool = False


@dataclass
class _Layout:
    lines: list[str]
    close: int
    top: dict[str, str] = field(default_factory=dict)
    meta: dict[str, str] = field(default_factory=dict)
    entries: list[_Entry] = field(default_factory=list)
    meta_line: Optional[int] = None
    meta_indent: Optional[int] = None
    meta_last: Optional[int] = None
    meta_bad: Optional[str] = None

    def find(self, key: str) -> Optional[_Entry]:
        """First entry for key, metadata block first (the same precedence as get)."""
        for scope in ("meta", "top"):
            for entry in self.entries:
                if entry.key == key and entry.scope == scope:
                    return entry
        return None

    def value(self, entry: _Entry) -> Optional[str]:
        if not entry.recorded:
            return None
        return (self.top if entry.scope == "top" else self.meta).get(entry.key)


def _bare(line: str) -> str:
    return line[:-1] if line.endswith("\r") else line


def _window(text: str) -> str:
    """Longest prefix of text that fits in WINDOW_BYTES of UTF-8."""
    head = text[:WINDOW_BYTES]
    raw = head.encode("utf-8", "surrogatepass")
    if len(raw) <= WINDOW_BYTES:
        return head
    return head[: len(raw[:WINDOW_BYTES].decode("utf-8", "ignore"))]


def _locate(text: str) -> tuple[list[str], int]:
    """Window lines and the index of the closing fence, or _ABSENT / _UNCLOSED."""
    window = _window(text)
    lines = window.split("\n")
    complete = lines if len(window) == len(text) else lines[:-1]
    first = _bare(complete[0]) if complete else ""
    if first.startswith(_BOM):
        first = first[1:]
    if first != _FENCE:
        return lines, _ABSENT
    for i in range(1, len(complete)):
        if _bare(complete[i]) == _FENCE:
            return lines, i
    return lines, _UNCLOSED


def _add_entry(lay: _Layout, scope: str, m: "re.Match[str]", i: int, indent: int) -> _Entry:
    entry = _Entry(scope, m.group(2), i, i, indent)
    lay.entries.append(entry)
    target = lay.top if scope == "top" else lay.meta
    value = scalar.value(m.group(3))
    if value is not None and entry.key not in target:
        target[entry.key] = value
        entry.recorded = True
    return entry


def _top_line(lay: _Layout, i: int, m: Optional["re.Match[str]"]) -> tuple[Optional[_Entry], bool]:
    """Handle a column-0 line; returns (entry, metadata block opened)."""
    if m is None:
        return None, False
    if m.group(2) != "metadata":
        return _add_entry(lay, "top", m, i, 0), False
    if not scalar.is_null(m.group(3)):
        lay.meta_bad = "'metadata' is not a block mapping"
    elif lay.meta_line is not None:
        lay.meta_bad = "duplicate 'metadata' key"
    else:
        lay.meta_line = lay.meta_last = i
        return None, True
    return None, False


def _meta_child(lay: _Layout, i: int, indent: int, m: Optional["re.Match[str]"]) -> Optional[_Entry]:
    if lay.meta_indent is None or indent < lay.meta_indent:
        lay.meta_bad = "inconsistent indentation in 'metadata'"
        return None
    if m is None:
        lay.meta_bad = "'metadata' is not a key: value mapping"
        return None
    return _add_entry(lay, "meta", m, i, indent)


def _continuation(lay: _Layout, prev: Optional[_Entry], i: int) -> None:
    """A deeper line belongs to the previous key, whose value is then multi-line: ignore it."""
    if prev is None:
        return
    prev.end = i
    if prev.recorded:
        (lay.top if prev.scope == "top" else lay.meta).pop(prev.key, None)
        prev.recorded = False


def _classify(lines: list[str], close: int) -> _Layout:
    lay = _Layout(lines, close)
    meta_open = False
    prev: Optional[_Entry] = None
    for i in range(1, close):
        line = _bare(lines[i])
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if not stripped or stripped.startswith("#"):
            if meta_open and stripped and indent > 0:
                lay.meta_last = i
            continue
        m = _KEY.match(line)
        if indent == 0:
            prev, meta_open = _top_line(lay, i, m)
            continue
        if meta_open:
            lay.meta_last = i
            if lay.meta_indent is None:
                lay.meta_indent = indent
            if indent <= lay.meta_indent:
                prev = _meta_child(lay, i, indent, m)
                continue
        _continuation(lay, prev, i)
    return lay


def parse(text: str) -> Frontmatter:
    """Parse the frontmatter at the start of text (DESIGN §3.1)."""
    lines, close = _locate(text)
    if close == _ABSENT:
        return Frontmatter({}, {}, False, True, None)
    if close == _UNCLOSED:
        return Frontmatter({}, {}, True, False, _UNCLOSED_MSG)
    lay = _classify(lines, close)
    return Frontmatter(lay.top, lay.meta, True, True, None)


def _format(key: str, value: Union[str, int]) -> str:
    if not _KEY_NAME.match(key) or key == "metadata":
        raise ValueError(f"invalid frontmatter key {key!r}")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TypeError(f"value for {key!r} must be str or int")
    if isinstance(value, int):
        return str(value)
    return '"' + sanitize(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _eol(text: str) -> str:
    nl = text.find("\n")
    return "\r\n" if nl > 0 and text[nl - 1] == "\r" else "\n"


def _insertion(lay: _Layout, lines: list[str], cr: str) -> tuple[int, int, list[str]]:
    if lay.meta_bad:
        raise PatchError(lay.meta_bad)
    if lay.meta_line is None or lay.meta_last is None:
        new = ["metadata:" + cr] + [" " * _INDENT + s + cr for s in lines]
        return lay.close, lay.close, new
    pad = " " * (lay.meta_indent or _INDENT)
    pos = lay.meta_last + 1
    return pos, pos, [pad + s + cr for s in lines]


def _plan(
    lay: _Layout, merged: dict[str, Union[str, int]], repl: Mapping[str, str], cr: str
) -> list[tuple[int, int, list[str]]]:
    edits: list[tuple[int, int, list[str]]] = []
    missing: list[str] = []
    for key, value in merged.items():
        entry = lay.find(key)
        line = f"{key}: {_format(key, value)}"
        if entry is None:
            missing.append(line)
        elif key in repl and not (entry.line == entry.end and lay.value(entry) == sanitize(str(value))):
            edits.append((entry.line, entry.end + 1, [" " * entry.indent + line + cr]))
    if missing:
        edits.append(_insertion(lay, missing, cr))
    return edits


def patch(
    text: str,
    add: Mapping[str, Union[str, int]],
    replace: Optional[Mapping[str, str]] = None,
) -> str:
    """Add missing keys to the metadata block; only 'tier' may be replaced (DESIGN §3.2)."""
    repl = dict(replace or {})
    for key in repl:
        if key not in _REPLACEABLE:
            raise ValueError(f"only {sorted(_REPLACEABLE)} can be replaced, not {key!r}")
    merged = dict(add)
    merged.update(repl)
    for key, value in merged.items():
        _format(key, value)
    lines, close = _locate(text)
    if close == _UNCLOSED:
        raise PatchError(_UNCLOSED_MSG)
    if not merged:
        return text
    if close == _ABSENT:
        bom = _BOM if text.startswith(_BOM) else ""
        eol = _eol(text)
        body = [f"{' ' * _INDENT}{k}: {_format(k, v)}" for k, v in merged.items()]
        return bom + eol.join([_FENCE, "metadata:"] + body + [_FENCE]) + eol + text[len(bom):]
    head = lines[: close + 1]
    edits = _plan(_classify(lines, close), merged, repl, "\r" if head[0].endswith("\r") else "")
    if not edits:
        return text
    new_head = list(head)
    for start, end, new in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        new_head[start:end] = new
    return "\n".join(new_head) + text[len("\n".join(head)):]
