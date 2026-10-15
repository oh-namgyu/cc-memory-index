"""Plan a migration from a hand-written MEMORY.md, without writing anything (DESIGN §8).

The index is cut into a preamble, segments and notes. A segment whose only link names a readable
memory file becomes that file's frontmatter (the first such segment per file); every other
segment becomes a manual line. Notes go verbatim into a new notes file (migrate_notes).
Rendering the plan in memory gives the would-be L0 and L1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AbstractSet, Mapping, Optional, Sequence, Union

from . import frontmatter
from .items import DEFAULT_SECTION, RESIDENT, Manual, ManualLine, PendingEntry, manual_bytes
from .links import memory_links
from .markdown import Verbatim
from .migrate_notes import Notes, notes_text
from .plan import render_store
from .render import Rendered
from .safeio import Fingerprint
from .segments import BULLET, pieces
from .store import Config, Source
from .text import quote, sanitize, split_segments, strip_bom

ORDER_STEP = 10
_SECTION = "## "
_SUB = "### "


@dataclass(frozen=True)
class IndexSegment:
    """A segment of a hand-written index line, its section and subsection, its order (10, 20, 30... in
    order of appearance) and the number of its line."""

    text: str
    section: str
    sub: str
    order: int
    line: int


@dataclass(frozen=True)
class Index:
    """A hand-written index cut up: preamble (None when blank), segments of the index lines, notes."""

    preamble: Optional[str]
    segments: list[IndexSegment]
    notes: Notes


@dataclass
class MigrationPlan:
    """What the migration writes: texts are the patched memory files; notes_name is the notes file,
    None when there are no notes. messages explain the segments kept as manual lines."""

    index: Index
    notes_name: Optional[str]
    manual: list[ManualLine] = field(default_factory=list)
    resident: list[str] = field(default_factory=list)
    texts: dict[str, str] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)

    def notes_file(self) -> Optional[tuple[str, str]]:
        """The name and text of the notes file, or None when there are no notes."""
        if self.notes_name is None:
            return None
        return self.notes_name, notes_text(self.notes_name, self.index.notes.body)


def _pieces(line: str) -> list[str]:
    return [piece for piece in map(sanitize, split_segments(line[len(BULLET):])) if piece]


def _kind(line: str, verbatim: Verbatim) -> str:
    """'section', 'sub', 'item' (a top-level list line with text), 'blank' or 'note'."""
    if verbatim.inside(line):
        return "note"
    if line.startswith(_SECTION):
        return "section"
    if line.startswith(_SUB):
        return "sub"
    if not line.strip():
        return "blank"
    return "item" if line.startswith(BULLET) and _pieces(line) else "note"


def _body_start(text: str, lines: Sequence[str]) -> int:
    """Index of the first '## ' heading or top-level list line outside code blocks and comments,
    after a frontmatter block at the start of text (found as for a memory file)."""
    _, close = frontmatter.locate(text)
    verbatim = Verbatim()
    return next((i for i in range(max(close + 1, 0), len(lines)) if _kind(lines[i], verbatim) in ("section", "item")),
                len(lines))


def split_index(text: str) -> Index:
    """The preamble, segments and notes of a hand-written index (LF line endings), in order.

    The preamble is everything before the first '## ' heading or top-level list line, kept
    verbatim (the renderer sanitises it line by line). After it, a top-level list line is split
    into segments, numbered 10, 20, 30... in order of appearance; '## ' and '### ' headings set the
    section and subsection; every other non-blank line is a note, and so is every line of a code
    block or an HTML comment, blank lines included.
    """
    lines = strip_bom(text).split("\n")
    if lines[-1] == "":
        lines.pop()
    start = _body_start(text, lines)
    preamble = "\n".join(lines[:start]).strip("\n")
    segments: list[IndexSegment] = []
    notes, verbatim = Notes(), Verbatim()
    section, sub = DEFAULT_SECTION, ""
    for number, line in enumerate(lines[start:], start + 1):
        kind = _kind(line, verbatim)
        if kind == "note":
            notes.add(line)
        elif kind == "section":
            section, sub = sanitize(line[len(_SECTION):]) or DEFAULT_SECTION, ""
            notes.heading(line, sub=False)
        elif kind == "sub":
            sub = sanitize(line[len(_SUB):])
            notes.heading(line, sub=True)
        else:
            notes.gap()
            for piece in _pieces(line) if kind == "item" else []:
                segments.append(IndexSegment(piece, section, sub, ORDER_STEP * (len(segments) + 1), number))
    return Index(preamble if preamble.strip() else None, segments, notes)


def _resident_text(src: Source, seg: IndexSegment) -> tuple[Optional[str], str]:
    """src's text with the segment as its resident idx, or None and the reason it cannot be."""
    add: dict[str, Union[str, int]] = {"tier": RESIDENT, "idx": seg.text, "idx_order": seg.order}
    # An absent idx_section or idx_sub means the same as an empty one (DESIGN §3), so neither is written empty.
    add.update({key: value for key, value in (("idx_section", seg.section), ("idx_sub", seg.sub)) if value})
    try:
        return frontmatter.patch_checked(src.text, add, check=("idx", "tier")), ""
    except frontmatter.PatchError as e:
        return None, f"its frontmatter cannot be patched ({e})"


def _assign(plan: MigrationPlan, seg: IndexSegment, by_name: Mapping[str, Source], blocked: AbstractSet[str],
            claimed: set[str]) -> bool:
    """Make seg its source's resident idx when it can be; False means a manual line."""
    links = memory_links(seg.text)
    if len(links) != 1:
        return False
    target = links[0]
    src = by_name.get(target)
    if src is None:
        reason = f"{quote(target)} is not a readable memory file in this store"
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
    plan.messages.append(f"MEMORY.md line {seg.line}: {reason}; kept as a manual line")
    return False


def make_plan(index_text: str, sources: Sequence[Source], blocked: AbstractSet[str], notes_name: str) -> MigrationPlan:
    """Plan the migration of index_text (LF line endings); blocked names are never patched, and
    notes_name is the name the notes file gets when there are notes."""
    index = split_index(index_text)
    plan = MigrationPlan(index, notes_name if index.notes.lines else None)
    by_name = {src.name: src for src in sources}
    claimed: set[str] = set()
    for seg in index.segments:
        if not _assign(plan, seg, by_name, blocked, claimed):
            plan.manual.append(ManualLine(seg.text, seg.section, seg.sub, seg.order))
    return plan


def manual_file(plan: MigrationPlan) -> bytes:
    """.index-manual.json holding the plan's preamble and manual lines (written even when both are empty)."""
    return manual_bytes(Manual(plan.index.preamble, list(plan.manual)))


def _source(name: str, text: str) -> Source:
    return Source(name, text, Fingerprint.of(text.encode("utf-8")), frontmatter.parse(text))


def preview(plan: MigrationPlan, sources: Sequence[Source], pending: Sequence[PendingEntry], cfg: Config) -> Rendered:
    """L0 and L1 as they would be rendered once the plan is applied."""
    patched = [_source(src.name, plan.texts[src.name]) if src.name in plan.texts else src for src in sources]
    notes = plan.notes_file()
    if notes is not None and notes[0] not in {src.name for src in sources}:
        patched.append(_source(*notes))
    return render_store(patched, Manual(plan.index.preamble, plan.manual), pending, cfg)[1]


def missing_segments(segments: Sequence[IndexSegment], rendered: Rendered) -> list[str]:
    """Original segments that are not in L0 or L1, compared in pieces as the rescue compares them."""
    found = set(rendered.pieces())
    return [seg.text for seg in segments if not all(p in found for p in pieces(seg.text))]
