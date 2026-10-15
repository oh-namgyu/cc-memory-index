"""Strict-subset frontmatter parser and insert-only patcher (DESIGN §3.1, §3.2).

Nothing here evaluates input: lines are classified with regular expressions and every line the
parser does not understand is left byte-for-byte as it was, or, when the patcher cannot be sure of
the structure around it, makes patch() refuse (PatchError) rather than add a second key or block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence, Union

from . import scalar
from .scalar import KEY, SEQ, entry_line
from .text import BOM, normalize_ws, sanitize, strip_bom, utf8_prefix

WINDOW_BYTES = 64 * 1024
_FENCE = "---"
# '---' followed by spaces or tabs: YAML starts a document there, some frontmatter readers end the
# block there and others do not. Where a fence could be, it makes the frontmatter unparseable.
_LOOSE_FENCE = re.compile(r"---[ \t]+\Z")
_YAML11_BREAK = re.compile("[\x85\u2028\u2029]")  # line breaks to YAML 1.1 and JavaScript readers
_ABSENT = -1
_UNCLOSED = -2
_LOOSE = -3
_BROKEN = {_UNCLOSED: "no closing '---' line within the first 64 KiB",
           _LOOSE: "a '---' line ends with spaces or tabs, so it is unclear where the frontmatter ends; remove them"}
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
        return self.metadata[key] if key in self.metadata else self.top.get(key)


@dataclass
class _Entry:
    scope: str
    key: str
    line: int
    end: int
    indent: int
    null: bool
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
    unknown: Optional[str] = None

    def doubt(self, i: int, why: str) -> None:
        """Record the first line whose structure the patcher cannot be sure of."""
        self.unknown = self.unknown or f"line {i + 1} {why}"

    def find(self, key: str) -> Optional[_Entry]:
        """First entry for key, metadata block first (the same precedence as get)."""
        for scope in ("meta", "top"):
            for entry in self.entries:
                if entry.key == key and entry.scope == scope:
                    return entry
        return None

    def value(self, entry: _Entry) -> Optional[str]:
        return (self.top if entry.scope == "top" else self.meta).get(entry.key) if entry.recorded else None


def _bare(line: str) -> str:
    return line[:-1] if line.endswith("\r") else line


def locate(text: str) -> tuple[list[str], int]:
    """Window lines and the index of the closing fence, or _ABSENT, _UNCLOSED or _LOOSE."""
    window = utf8_prefix(text, WINDOW_BYTES)
    lines = window.split("\n")
    complete = lines if len(window) == len(text) else lines[:-1]
    first = strip_bom(_bare(complete[0])) if complete else ""
    if first != _FENCE:
        return lines, _LOOSE if _LOOSE_FENCE.match(first) else _ABSENT
    for i in range(1, len(complete)):
        line = _bare(complete[i])
        if line == _FENCE:
            return lines, i
        if _LOOSE_FENCE.match(line):
            return lines, _LOOSE
    return lines, _UNCLOSED


def _add_entry(lay: _Layout, scope: str, m: "re.Match[str]", i: int, indent: int) -> _Entry:
    entry = _Entry(scope, m.group(2), i, i, indent, scalar.is_null(m.group(3)))
    lay.entries.append(entry)
    if scalar.unclosed(m.group(3)):
        lay.doubt(i, "opens a quoted value that does not end on that line")
    target = lay.top if scope == "top" else lay.meta
    value = scalar.value(m.group(3))
    if value is not None and entry.key not in target:
        target[entry.key] = value
        entry.recorded = True
    return entry


def _top_line(
    lay: _Layout, i: int, line: str, m: Optional["re.Match[str]"], prev: Optional[_Entry]
) -> tuple[Optional[_Entry], bool]:
    """Handle a column-0 line; returns (the entry later deeper lines belong to, metadata block opened).

    A '- ' line at column 0 is understood only as the block sequence value of the null-valued key
    before it; any other column-0 line that is not 'key: value' makes the structure unknown.
    """
    if m is None:
        if prev is None or not prev.null or not SEQ.match(line):
            lay.doubt(i, "is neither 'key: value' nor a comment")
            return None, False
        if scalar.unclosed(scalar.item_value(line)):
            lay.doubt(i, "opens a quoted value that does not end on that line")
        _continuation(lay, prev, i)
        return prev, False
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
        if _YAML11_BREAK.search(line):
            lay.doubt(i, "holds NEL, U+2028 or U+2029, which some YAML readers take as a line break")
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if not stripped or stripped.startswith("#"):
            if meta_open and stripped and indent > 0:
                lay.meta_last = i
            continue
        m = KEY.match(line)
        if indent == 0:
            prev, meta_open = _top_line(lay, i, line, m, prev)
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
    lines, close = locate(text)
    if close == _ABSENT:
        return Frontmatter({}, {}, False, True, None)
    if close in _BROKEN:
        return Frontmatter({}, {}, True, False, _BROKEN[close])
    lay = _classify(lines, close)
    return Frontmatter(lay.top, lay.meta, True, True, None)


def _eol(text: str) -> str:
    nl = text.find("\n")
    return "\r\n" if nl > 0 and text[nl - 1] == "\r" else "\n"


def _insertion(lay: _Layout, lines: list[str], cr: str) -> tuple[int, int, list[str]]:
    if lay.meta_line is None or lay.meta_last is None:
        new = ["metadata:" + cr] + [" " * _INDENT + s + cr for s in lines]
        return lay.close, lay.close, new
    pos = lay.meta_last + 1
    return pos, pos, [" " * (lay.meta_indent or _INDENT) + s + cr for s in lines]


def _plan(
    lay: _Layout, merged: dict[str, Union[str, int]], repl: Mapping[str, str], cr: str
) -> list[tuple[int, int, list[str]]]:
    edits: list[tuple[int, int, list[str]]] = []
    missing: list[str] = []
    for key, value in merged.items():
        entry = lay.find(key)
        line = entry_line(key, value)
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
    merged = {**add, **repl}
    for key, value in merged.items():
        entry_line(key, value)
    lines, close = locate(text)
    if close in _BROKEN:
        raise PatchError(_BROKEN[close])
    if not merged:
        return text
    if close == _ABSENT:
        return _new_block(text, merged)
    lay = _classify(lines, close)
    problem = lay.meta_bad or lay.unknown
    if problem is not None:
        raise PatchError(problem)
    head = lines[: close + 1]
    edits = _plan(lay, merged, repl, "\r" if head[0].endswith("\r") else "")
    if not edits:
        return text
    new_head = list(head)
    for start, end, new in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        new_head[start:end] = new
    return "\n".join(new_head) + text[len("\n".join(head)):]


def _new_block(text: str, merged: Mapping[str, Union[str, int]]) -> str:
    """text without frontmatter, after a new block holding only metadata (BOM and line ending kept)."""
    bom = BOM if text.startswith(BOM) else ""
    eol = _eol(text)
    body = [" " * _INDENT + entry_line(k, v) for k, v in merged.items()]
    return bom + eol.join([_FENCE, "metadata:"] + body + [_FENCE]) + eol + text[len(bom):]


def _norm(value: Optional[str]) -> str:
    return normalize_ws(sanitize(value or ""))


def patch_checked(text: str, add: Mapping[str, Union[str, int]], replace: Optional[Mapping[str, str]] = None,
                  check: Sequence[str] = ()) -> str:
    """patch(), then parse the result again: PatchError unless it parses and every key in check reads
    back as the value asked for. Every command that patches a memory file writes only this result."""
    out = patch(text, add, replace)
    fm = parse(out)
    if not fm.ok:
        raise PatchError(f"the patched frontmatter would not parse: {fm.error}")
    wanted = {**add, **(replace or {})}
    for key in check:
        if _norm(fm.get(key)) != _norm(str(wanted[key])):
            raise PatchError(f"its {key!r} key already holds a different, empty or non-plain value")
    return out
