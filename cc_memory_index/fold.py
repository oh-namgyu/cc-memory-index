"""Write rescued lines and fold pending entries into frontmatter, in the order of DESIGN §6.1.

(a) .index-pending.json gains the new entries, (b) .index-manual.json the new lines, (c) each
pending entry whose file has no idx is patched into that file, (d) .index-pending.json loses
the entries now folded. Every write replaces one file against the fingerprint read, so a crash
between two writes leaves a valid store and the next build finishes the job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence, Union

from .frontmatter import PatchError, parse, patch
from .items import MANUAL_NAME, PENDING_NAME, Manual, ManualLine, PendingEntry, manual_bytes, pending_bytes
from .safeio import ConcurrentEdit, Fingerprint, StoreDir, UnsafeEntry
from .segments import norm
from .store import Source
from .text import sanitize


@dataclass
class DataFiles:
    """.index-manual.json and .index-pending.json as read under the lock (fp None: absent)."""

    manual: Manual
    manual_fp: Optional[Fingerprint]
    pending: list[PendingEntry]
    pending_fp: Optional[Fingerprint]


@dataclass
class Folded:
    """What fold() did: written lists every file replaced, concurrent the files that changed
    under it (their entries stay pending), messages the entries that could not be folded."""

    written: list[str] = field(default_factory=list)
    concurrent: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def wrote(self, name: str) -> None:
        if name not in self.written:
            self.written.append(name)


def folded_text(src: Source, entry: PendingEntry) -> str:
    """src's text with the entry's idx, section, sub and order added to its frontmatter (DESIGN §6.1
    step 2). PatchError when that cannot be done safely or would not give the file this idx."""
    add: dict[str, Union[str, int]] = {
        "idx": entry.idx, "idx_section": entry.section, "idx_sub": entry.sub, "idx_order": entry.order,
    }
    text = patch(src.text, add)
    if norm(parse(text).get("idx") or "") != norm(entry.idx):
        raise PatchError("it already has an 'idx' key that is empty or not a plain value")
    return text


def _patch(sd: StoreDir, src: Source, entry: PendingEntry, out: Folded) -> bool:
    """Write folded_text into src; True when written."""
    try:
        sd.replace(src.name, folded_text(src, entry).encode("utf-8"), src.fp)
    except ConcurrentEdit:
        out.concurrent.append(src.name)
        return False
    except (PatchError, UnsafeEntry) as e:
        out.messages.append(
            f"{src.name}: the rescued index line could not be added to its frontmatter ({e}); it stays in"
            f" {PENDING_NAME} and is still listed. Fix the frontmatter and run build again"
        )
        return False
    out.wrote(src.name)
    return True


def _fold_entries(
    sd: StoreDir, pending: Sequence[PendingEntry], sources: Mapping[str, Source], out: Folded
) -> list[PendingEntry]:
    """(c): the entries to keep. An entry equal to its file's idx is folded already and dropped;
    the first entry for a file without idx is patched in; every other entry is kept (DESIGN §6.2)."""
    idx_of = {name: sanitize(src.fm.get("idx") or "") for name, src in sources.items()}
    keep: list[PendingEntry] = []
    for entry in pending:
        current = idx_of.get(entry.file)
        if current is None or (current and norm(current) != norm(entry.idx)):
            keep.append(entry)
        elif not current:
            idx_of[entry.file] = sanitize(entry.idx)
            if not _patch(sd, sources[entry.file], entry, out):
                keep.append(entry)
    return keep


def fold(
    sd: StoreDir, data: DataFiles, sources: Sequence[Source], entries: Sequence[PendingEntry],
    lines: Sequence[ManualLine], out: Folded,
) -> None:
    """Steps (a)-(d) of DESIGN §6.1 for the new entries and lines, then for every pending entry."""
    pending, fp = data.pending + list(entries), data.pending_fp
    if entries:
        fp = sd.replace(PENDING_NAME, pending_bytes(pending), fp)
        out.wrote(PENDING_NAME)
    if lines:
        manual = Manual(data.manual.preamble, data.manual.lines + list(lines))
        sd.replace(MANUAL_NAME, manual_bytes(manual), data.manual_fp)
        out.wrote(MANUAL_NAME)
    keep = _fold_entries(sd, pending, {src.name: src for src in sources}, out)
    if len(keep) != len(pending):
        sd.replace(PENDING_NAME, pending_bytes(keep), fp)
        out.wrote(PENDING_NAME)
