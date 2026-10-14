"""Index items from sources, manual lines and pending entries (DESIGN §3, §4, §6).

An Item's origin says where its place in the output comes from:

* 'source': a memory file. Its idx comes from the frontmatter, else from a pending entry
  for that file (a rescued line not folded yet), else from the default of DESIGN §3.
* 'manual': a line of .index-manual.json that links to no memory file. Always resident.
* 'pending': a pending entry that is not the file's idx (a conflict, a second entry for the
  same file, or a file that is not a readable source). Rendered under L1 'Unresolved'.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional, Sequence

from .safeio import SafeIOError, StoreDir
from .store import Source, _reject_duplicates, valid_source_name
from .text import normalize_ws, sanitize

MANUAL_NAME = ".index-manual.json"
PENDING_NAME = ".index-pending.json"
DATA_MAX_BYTES = 1024 * 1024
DATA_VERSION = 1
DEFAULT_ORDER = 1_000_000
DEFAULT_SECTION = "Other"
RESIDENT = "resident"
ORDER_LIMIT = 10**18
_ORDER_TEXT = re.compile(r"[+-]?[0-9]{1,18}\Z")
_MANUAL_KEYS = ("version", "preamble", "lines")
_LINE_KEYS = ("seg", "section", "sub", "order")
_PENDING_KEYS = ("version", "entries")
_ENTRY_KEYS = ("file", "idx", "section", "sub", "order")


class DataFileError(Exception):
    """.index-manual.json or .index-pending.json is unreadable or malformed (exit 2)."""


@dataclass(frozen=True)
class Item:
    file: Optional[str]
    idx: str
    section: str
    sub: str
    order: int
    resident: bool
    origin: str


@dataclass(frozen=True)
class ManualLine:
    seg: str
    section: str
    sub: str
    order: int


@dataclass(frozen=True)
class Manual:
    preamble: Optional[str]
    lines: list[ManualLine]


@dataclass(frozen=True)
class PendingEntry:
    file: str
    idx: str
    section: str
    sub: str
    order: int


def _read_object(sd: StoreDir, name: str) -> Optional[dict[str, object]]:
    """Strict JSON object from a store data file, or None when the file is absent."""
    if sd.kind(name) == "missing":
        return None
    try:
        data, _ = sd.read(name, DATA_MAX_BYTES)
        obj = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_reject_duplicates)
    except SafeIOError as e:
        raise DataFileError(f"cannot read {name}: {e}") from e
    except (ValueError, RecursionError) as e:
        raise DataFileError(f"{name} is not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise DataFileError(f"{name} must contain a JSON object")
    return obj


def _fields(obj: object, keys: tuple[str, ...], where: str) -> dict[str, object]:
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise DataFileError(f"{where} must be an object with exactly the keys {', '.join(keys)}")
    return obj


def _text(obj: dict[str, object], key: str, where: str, non_empty: bool) -> str:
    value = obj[key]
    if not isinstance(value, str):
        raise DataFileError(f"{where}.{key} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as e:
        raise DataFileError(f"{where}.{key} is not valid Unicode text") from e
    if non_empty and not sanitize(value):
        raise DataFileError(f"{where}.{key} is empty")
    return value


def _order(obj: dict[str, object], where: str) -> int:
    value = obj["order"]
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) >= ORDER_LIMIT:
        raise DataFileError(f"{where}.order must be an integer below {ORDER_LIMIT:.0e} in magnitude")
    return value


def _entries(obj: dict[str, object], name: str, keys: tuple[str, ...], list_key: str) -> list[dict[str, object]]:
    _fields(obj, keys, name)
    version = obj["version"]
    if type(version) is not int or version != DATA_VERSION:
        raise DataFileError(f"{name}: unsupported version {version!r} (expected {DATA_VERSION})")
    entries = obj[list_key]
    if not isinstance(entries, list):
        raise DataFileError(f"{name}: '{list_key}' must be a list")
    return entries


def load_manual(sd: StoreDir) -> Manual:
    """The manual preamble and lines; an absent file means none."""
    obj = _read_object(sd, MANUAL_NAME)
    if obj is None:
        return Manual(None, [])
    lines: list[ManualLine] = []
    for i, raw in enumerate(_entries(obj, MANUAL_NAME, _MANUAL_KEYS, "lines")):
        where = f"{MANUAL_NAME} lines[{i}]"
        entry = _fields(raw, _LINE_KEYS, where)
        lines.append(ManualLine(
            _text(entry, "seg", where, True),
            _text(entry, "section", where, False),
            _text(entry, "sub", where, False),
            _order(entry, where),
        ))
    preamble = obj["preamble"]
    if preamble is not None:
        preamble = _text(obj, "preamble", MANUAL_NAME, False)
    return Manual(preamble, lines)


def load_pending(sd: StoreDir) -> list[PendingEntry]:
    """Rescued lines waiting to be folded into frontmatter; an absent file means none."""
    obj = _read_object(sd, PENDING_NAME)
    if obj is None:
        return []
    out: list[PendingEntry] = []
    for i, raw in enumerate(_entries(obj, PENDING_NAME, _PENDING_KEYS, "entries")):
        where = f"{PENDING_NAME} entries[{i}]"
        entry = _fields(raw, _ENTRY_KEYS, where)
        file = _text(entry, "file", where, True)
        if not valid_source_name(file):
            raise DataFileError(f"{where}.file is not a valid source name: {file!r}")
        out.append(PendingEntry(
            file,
            _text(entry, "idx", where, True),
            _text(entry, "section", where, False),
            _text(entry, "sub", where, False),
            _order(entry, where),
        ))
    return out


def _value(src: Source, key: str) -> str:
    return sanitize(src.fm.get(key) or "")


def _default_idx(src: Source) -> str:
    name = _value(src, "name") or src.name[: -len(".md")]
    description = _value(src, "description")
    link = f"[{name}]({src.name})"
    return f"{link} — {description}" if description else link


def _type_section(src: Source) -> str:
    kind = _value(src, "type")
    return kind[:1].upper() + kind[1:] if kind else DEFAULT_SECTION


def _source_order(src: Source, problems: list[str]) -> int:
    raw = src.fm.get("idx_order")
    if raw is None or not raw.strip():
        return DEFAULT_ORDER
    if _ORDER_TEXT.match(raw.strip()):
        return int(raw.strip())
    problems.append(f"{src.name}: idx_order {raw[:40]!r} is not an integer; using {DEFAULT_ORDER:,}")
    return DEFAULT_ORDER


def _source_item(src: Source, pending: Optional[PendingEntry], problems: list[str]) -> Item:
    explicit = _value(src, "idx")
    resident = _value(src, "tier") == RESIDENT
    if pending is not None and not explicit:
        section = sanitize(pending.section) or _type_section(src)
        return Item(src.name, sanitize(pending.idx), section, sanitize(pending.sub), pending.order, resident, "source")
    section = _value(src, "idx_section") or _type_section(src)
    order = _source_order(src, problems)
    return Item(src.name, explicit or _default_idx(src), section, _value(src, "idx_sub"), order, resident, "source")


def _pending_item(entry: PendingEntry) -> Item:
    section = sanitize(entry.section) or DEFAULT_SECTION
    return Item(entry.file, sanitize(entry.idx), section, sanitize(entry.sub), entry.order, False, "pending")


def items_from_sources(
    sources: Sequence[Source], pending: Sequence[PendingEntry], problems: Optional[list[str]] = None
) -> list[Item]:
    """One item per source, then every pending entry that is not its file's idx (DESIGN §6.1-§6.2).

    A pending entry stands in for the idx of a file that has none. One that equals the file's
    idx (whitespace-normalised) is already folded and adds nothing. Anything else is kept as
    an 'Unresolved' item: nothing is discarded automatically. Problems are appended as messages.
    """
    notes: list[str] = [] if problems is None else problems
    first: dict[str, PendingEntry] = {}
    for entry in pending:
        first.setdefault(entry.file, entry)
    items: list[Item] = []
    idx_of: dict[str, str] = {}
    for src in sources:
        item = _source_item(src, first.get(src.name), notes)
        items.append(item)
        idx_of[src.name] = normalize_ws(item.idx)
    for entry in pending:
        current = idx_of.get(entry.file)
        if current == normalize_ws(sanitize(entry.idx)):
            continue
        if current is None:
            notes.append(f"{PENDING_NAME}: {entry.file} is not a readable memory file; its line stays under Unresolved")
        items.append(_pending_item(entry))
    return items


def manual_items(manual: Manual) -> list[Item]:
    """Manual lines as items; they link to no memory file and are always resident candidates."""
    return [
        Item(None, sanitize(line.seg), sanitize(line.section) or DEFAULT_SECTION, sanitize(line.sub), line.order,
             True, "manual")
        for line in manual.lines
    ]
