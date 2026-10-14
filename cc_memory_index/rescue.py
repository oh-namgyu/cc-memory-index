"""Hand edits of the generated files: find the added segments and decide where each goes (DESIGN §6).

Nothing here reads or writes files. analyse() classifies every segment of an edited L0 or L1
that the fresh render does not contain; lost() is the run-time invariant checked before the
generated files are replaced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Optional, Sequence

from .items import DEFAULT_SECTION, MANUAL_NAME, ORDER_LIMIT, PENDING_NAME, ManualLine, PendingEntry, default_idx
from .render import Rendered, RenderInvariant, is_warning_line, l1_head, link_line, preamble_lines
from .segments import Seg, body_segments, norm, pieces, split_head
from .store import Source, valid_source_name
from .text import sanitize

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
HINT = "edit the memory file's frontmatter (metadata.idx) instead"


@dataclass(frozen=True)
class Edited:
    """A generated file whose content differs from what this tool wrote; text is None when not UTF-8."""

    name: str
    role: str
    text: Optional[str]


@dataclass(frozen=True)
class Context:
    """What the classification needs from the store as read under the lock. defaults holds the
    pieces of every source's default line, which is generated even when a pending line or an
    idx is what gets rendered."""

    rendered: Rendered
    preamble: Optional[str]
    l1_names: tuple[str, ...]
    sources: Mapping[str, Source]
    pending_files: frozenset[str]
    max_order: int
    defaults: frozenset[str]


@dataclass(frozen=True)
class Unrescuable:
    file: str
    segment: str
    reason: str


@dataclass
class Rescue:
    """New pending entries and manual lines, or the reasons nothing may be written.

    pieces holds the normalised segments of each edited file's body, keyed by role.
    """

    entries: list[PendingEntry] = field(default_factory=list)
    lines: list[ManualLine] = field(default_factory=list)
    unrescuable: list[Unrescuable] = field(default_factory=list)
    pieces: dict[str, list[str]] = field(default_factory=dict)


def memory_links(text: str) -> list[str]:
    """Link targets that name a .md file (URLs with a scheme are not memory links)."""
    return [t for t in _LINK.findall(text) if t.endswith(".md") and not _URL.match(t)]


def default_pieces(sources: Iterable[Source]) -> frozenset[str]:
    """Pieces of the default line of every source (DESIGN §3), generated whatever is rendered."""
    return frozenset(p for src in sources for p in pieces(default_idx(src)))


def _no_extra(line: str) -> bool:
    return False


def _l0_head(preamble: Optional[str], l1_names: Sequence[str]) -> tuple[list[str], Callable[[str], bool]]:
    """What an L0 must start with (the manual preamble), and a test for its generated head lines,
    which may be missing (DESIGN §6: deleted generated lines need no action): the default
    preamble, the L1 link line and the budget warning line with any count."""
    lines = preamble_lines(preamble)
    default = lines == preamble_lines(None)
    generated = {norm(link_line(name)) for name in l1_names} | ({norm(line) for line in lines} if default else set())
    return ([] if default else lines), lambda line: norm(line) in generated or is_warning_line(line, l1_names)


def _split(text: str, l1: bool, preamble: Optional[str], l1_names: Sequence[str]) -> tuple[Optional[list[str]], str]:
    lines = text.lstrip("\ufeff").split("\n")
    if l1:
        return split_head(lines, l1_head(l1_names[0]), _no_extra)
    return split_head(lines, *_l0_head(preamble, l1_names))


def body_pieces(rendered: Rendered, preamble: Optional[str], l1_name: str) -> set[str]:
    """Normalised segments of the bodies of rendered L0 and L1 (their heads excluded)."""
    out: set[str] = set()
    for text, l1 in ((rendered.l0, False), (rendered.l1, True)):
        rest, _ = _split(text, l1, preamble, (l1_name,))
        if rest is None:
            raise RenderInvariant("a rendered file does not start with its head")
        out.update(p for seg in body_segments(rest, l1) for p in pieces(seg.text))
    return out


def _body(ed: Edited, ctx: Context, out: Rescue) -> Optional[list[Seg]]:
    """The segments after the head, or None (recorded as unrescuable) when the head was edited."""
    if ed.text is None:
        out.unrescuable.append(Unrescuable(ed.name, "", "not valid UTF-8; restore it or run `build --force`"))
        return None
    l1 = ed.role == "l1"
    rest, differs = _split(ed.text, l1, ctx.preamble, ctx.l1_names)
    if rest is None:
        reason = ("the generated header was edited; restore it or run `build --force`" if l1
                  else f"preamble edited — change it in {MANUAL_NAME}")
        out.unrescuable.append(Unrescuable(ed.name, differs, reason))
        return None
    return body_segments(rest, l1)


def _refusal(links: Sequence[str], ctx: Context, claimed: set[str]) -> Optional[str]:
    """Why a segment that links to memory files cannot be rescued, or None when it can."""
    if len(links) > 1:
        return f"links to {len(links)} memory files, so it cannot be one file's index line; {HINT}"
    target = links[0]
    if not valid_source_name(target):
        return f"link target {target[:80]!r} is not a memory file in this directory; {HINT}"
    src = ctx.sources.get(target)
    if src is None:
        return f"{target} does not exist or could not be read; {HINT}"
    if sanitize(src.fm.get("idx") or ""):
        return f"{target} already has an index line (metadata.idx), so this is an edited generated line; {HINT}"
    if target in ctx.pending_files:
        return f"{target} already has a line waiting in {PENDING_NAME}; {HINT}"
    if target in claimed:
        return f"another added line already links to {target}; {HINT}"
    return None


def _added(edited: Sequence[Edited], ctx: Context, out: Rescue) -> list[tuple[str, Seg]]:
    """Segments of the edited files that are neither in the fresh render nor a source's default
    line, first occurrence of each."""
    known = body_pieces(ctx.rendered, ctx.preamble, ctx.l1_names[0]) | ctx.defaults
    added: list[tuple[str, Seg]] = []
    seen: set[str] = set()
    for ed in edited:
        segs = _body(ed, ctx, out)
        if segs is None:
            continue
        out.pieces[ed.role] = [p for seg in segs for p in pieces(seg.text)]
        for seg in segs:
            key = norm(seg.text)
            if key not in seen and not all(p in known for p in pieces(seg.text)):
                seen.add(key)
                added.append((ed.name, seg))
    return added


def analyse(edited: Sequence[Edited], ctx: Context) -> Rescue:
    """Classify the added segments (DESIGN §6 table).

    Exactly one memory link, to a parsed source without idx or pending entry that no earlier
    segment claimed: a pending entry. No memory link: a manual line where it appeared (no
    section under L1's Overflow and Unresolved). Anything else is unrescuable. New items are
    ordered after every existing one: pending entries first, then manual lines, each in order
    of appearance, so a rerun after a crash between the two writes assigns the same orders.
    """
    out = Rescue()
    claimed: set[str] = set()
    linked: list[tuple[str, Seg]] = []
    unlinked: list[Seg] = []
    for name, seg in _added(edited, ctx, out):
        links = memory_links(seg.text)
        reason = _refusal(links, ctx, claimed) if links else None
        if reason is not None:
            out.unrescuable.append(Unrescuable(name, seg.text, reason))
        elif links:
            claimed.add(links[0])
            linked.append((links[0], seg))
        else:
            unlinked.append(seg)
    order = ctx.max_order
    for file, seg in linked:
        order = min(order + 1, ORDER_LIMIT - 1)
        # Claude appends at the end of MEMORY.md, so the position says nothing about the
        # section: leave it empty and let the memory file's type decide.
        out.entries.append(PendingEntry(file, sanitize(seg.text), "", "", order))
    for seg in unlinked:
        order = min(order + 1, ORDER_LIMIT - 1)
        out.lines.append(ManualLine(sanitize(seg.text), seg.section or DEFAULT_SECTION, seg.sub, order))
    return out


def lost(
    current: Sequence[tuple[Optional[str], Optional[list[str]]]], fresh: set[str], new: set[str], generated: set[str]
) -> list[str]:
    """Segments of the current generated files that the new render would lose (DESIGN §6 invariant).

    current holds (text, pieces) per file: the pieces of an edited file's body as analysed, or
    None for a file this tool wrote, where only lines of the fresh render count (lines of
    sources removed since are not losses). generated holds lines that may go because they were
    derived from a source: default lines, and the lines of sources that changed during the run.
    """
    missing: list[str] = []
    for text, analysed in current:
        if analysed is not None:
            missing += [p for p in analysed if p not in new and p not in generated]
        elif text is not None:
            found = [p for seg in body_segments(text.split("\n"), False) for p in pieces(seg.text)]
            missing += [p for p in found if p in fresh and p not in new and p not in generated]
    return missing
