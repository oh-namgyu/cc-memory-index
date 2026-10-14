"""Plan a migration from a hand-written MEMORY.md, without writing anything (DESIGN §8).

The index is cut into a preamble and segments. A segment whose only link names a readable
memory file becomes that file's frontmatter (the first such segment per file); every other
segment becomes a manual line. Rendering the plan in memory gives the would-be L0 and L1.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import AbstractSet, Mapping, Optional, Sequence, Union

from . import frontmatter
from .items import (
    DEFAULT_SECTION, RESIDENT, Manual, ManualLine, PendingEntry, items_from_sources, manual_items,
)
from .items import manual_bytes as manual_file_bytes
from .render import BULLET, Rendered, render
from .safeio import Fingerprint
from .store import Config, Source
from .text import normalize_ws, sanitize, split_segments

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
ORDER_STEP = 10
_BOM = "\ufeff"
_FENCE = "---"
_SECTION = "## "
_SUB = "### "


@dataclass(frozen=True)
class Segment:
    text: str
    section: str
    sub: str
    order: int
    line: int


@dataclass
class Plan:
    preamble: Optional[str]
    segments: list[Segment]
    manual: list[ManualLine] = field(default_factory=list)
    resident: list[str] = field(default_factory=list)
    texts: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _body_start(lines: Sequence[str]) -> int:
    """Index of the first '## ' heading or top-level list line, after a leading '---' block."""
    first = 0
    if lines and lines[0] == _FENCE:
        first = next((i + 1 for i in range(1, len(lines)) if lines[i] == _FENCE), 0)
    for i in range(first, len(lines)):
        if lines[i].startswith(_SECTION) or lines[i].startswith(BULLET):
            return i
    return len(lines)


def _pieces(line: str) -> list[str]:
    body = line.strip()
    raw = split_segments(body[len(BULLET):]) if body.startswith(BULLET) else [body]
    return [piece for piece in map(sanitize, raw) if piece]


def split_index(text: str) -> tuple[Optional[str], list[Segment]]:
    """Preamble (None when blank) and the segments of a hand-written index, in order.

    The preamble is everything before the first '## ' heading or top-level list line, kept
    verbatim (the renderer sanitises it line by line). List lines are split into segments;
    any other non-blank line is one segment. Orders are 10, 20, 30... in order of appearance.
    """
    lines = text[len(_BOM):].split("\n") if text.startswith(_BOM) else text.split("\n")
    start = _body_start(lines)
    preamble = "\n".join(lines[:start]).strip("\n")
    segments: list[Segment] = []
    section, sub = DEFAULT_SECTION, ""
    for number, line in enumerate(lines[start:], start + 1):
        if line.startswith(_SECTION):
            section, sub = sanitize(line[len(_SECTION):]) or DEFAULT_SECTION, ""
        elif line.startswith(_SUB):
            sub = sanitize(line[len(_SUB):])
        else:
            for piece in _pieces(line):
                segments.append(Segment(piece, section, sub, ORDER_STEP * (len(segments) + 1), number))
    return (preamble if preamble.strip() else None), segments


def memory_links(text: str) -> list[str]:
    """Link targets that name a .md file; URLs with a scheme are not memory links."""
    return [t for t in LINK.findall(text) if t.endswith(".md") and not URL.match(t)]


def _resident_text(src: Source, seg: Segment) -> tuple[Optional[str], str]:
    """src's text with the segment as its resident idx, or None and the reason it cannot be."""
    add: dict[str, Union[str, int]] = {"tier": RESIDENT, "idx": seg.text, "idx_section": seg.section,
                                       "idx_sub": seg.sub, "idx_order": seg.order}
    try:
        text = frontmatter.patch(src.text, add)
    except frontmatter.PatchError as e:
        return None, f"its frontmatter cannot be patched ({e})"
    fm = frontmatter.parse(text)
    if normalize_ws(sanitize(fm.get("idx") or "")) != normalize_ws(seg.text):
        return None, "it already has a different idx"
    if sanitize(fm.get("tier") or "") != RESIDENT:
        return None, "it already has a different tier"
    return text, ""


def _assign(plan: Plan, seg: Segment, by_name: Mapping[str, Source], blocked: AbstractSet[str],
            claimed: set[str]) -> bool:
    """Make seg its source's resident idx when it can be; False means a manual line."""
    links = memory_links(seg.text)
    if len(links) != 1:
        return False
    target = links[0]
    src = by_name.get(target)
    if src is None:
        reason = f"{target!r} is not a readable memory file in this store"
    elif target in claimed:
        reason = f"an earlier line already links {target}"
    elif target in blocked:
        reason = f"{target} has more than one hard link and is never written"
    else:
        claimed.add(target)
        text, reason = _resident_text(src, seg)
        if text is not None:
            plan.resident.append(target)
            if text != src.text:
                plan.texts[target] = text
            return True
    plan.notes.append(f"MEMORY.md line {seg.line}: {reason}; kept as a manual line")
    return False


def make_plan(index_text: str, sources: Sequence[Source], blocked: AbstractSet[str]) -> Plan:
    """Plan the migration of index_text (LF line endings); blocked names are never patched."""
    preamble, segments = split_index(index_text)
    plan = Plan(preamble, segments)
    by_name = {src.name: src for src in sources}
    claimed: set[str] = set()
    for seg in segments:
        if not _assign(plan, seg, by_name, blocked, claimed):
            plan.manual.append(ManualLine(seg.text, seg.section, seg.sub, seg.order))
    return plan


def manual_bytes(plan: Plan) -> bytes:
    """.index-manual.json holding the plan's preamble and manual lines."""
    return manual_file_bytes(Manual(plan.preamble, list(plan.manual)))


def render_store(sources: Sequence[Source], manual: Manual, pending: Sequence[PendingEntry],
                 cfg: Config) -> Rendered:
    items = items_from_sources(sources, pending) + manual_items(manual)
    return render(items, manual.preamble, cfg)


def _patched(src: Source, text: str) -> Source:
    data = text.encode("utf-8")
    fp = Fingerprint(len(data), hashlib.sha256(data).hexdigest())
    return Source(src.name, text, fp, frontmatter.parse(text))


def preview(plan: Plan, sources: Sequence[Source], pending: Sequence[PendingEntry], cfg: Config) -> Rendered:
    """L0 and L1 as they would be rendered once the plan is applied."""
    patched = [_patched(src, plan.texts[src.name]) if src.name in plan.texts else src for src in sources]
    return render_store(patched, Manual(plan.preamble, plan.manual), pending, cfg)


def _rendered_segments(rendered: Rendered) -> set[str]:
    found: set[str] = set()
    for text in (rendered.l0, rendered.l1):
        for line in text.split("\n"):
            pieces = split_segments(line[len(BULLET):]) if line.startswith(BULLET) else [line]
            found.update(normalize_ws(piece) for piece in pieces)
    return found


def missing_segments(segments: Sequence[Segment], rendered: Rendered) -> list[str]:
    """Original segments that are not in L0 or L1 (whitespace-normalised)."""
    found = _rendered_segments(rendered)
    return [seg.text for seg in segments if not all(normalize_ws(p) in found for p in split_segments(seg.text))]
