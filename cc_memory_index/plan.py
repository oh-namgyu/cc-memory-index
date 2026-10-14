"""Read a store under its lock and render it; nothing here writes (DESIGN §4, §6, §8)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .fold import DataFiles
from .generated import (
    MIGRATING_MARK,
    RECORD_MAX_BYTES,
    Current,
    Record,
    needs_migration,
    read_current,
    read_record,
    stale_generated,
)
from .items import Item, Manual, items_from_sources, manual_items, read_manual, read_pending
from .render import Rendered, RenderInvariant, nbytes, reachable, render
from .safeio import StoreDir
from .store import L0_NAME, RENDER_MARK, Config, ScanResult, scan, valid_source_name


class Refusal(Exception):
    """A command declines to go on; nothing has been written."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Inputs:
    """Everything the render is made from: sources, data files and stale earlier L1 files."""

    scanned: ScanResult
    data: DataFiles
    stale: dict[str, str]


@dataclass
class Plan:
    """The store as read and rendered. l0, l1 and record keep the content and fingerprints first
    read, also after a re-render, so a concurrent edit of them is still detected on write."""

    inputs: Inputs
    items: list[Item]
    rendered: Rendered
    l0: Current
    l1: Current
    record: Current
    parsed: Optional[Record]
    parse_failed: bool


def skip_notes(scanned: ScanResult) -> list[str]:
    """One note per memory file that was skipped or whose frontmatter cannot be parsed (exit 7).

    Badly named entries are not memory files and are left to lint (DESIGN §2). A source whose
    frontmatter cannot be parsed is still listed in L1 with its default line, so it stays reachable.
    """
    notes = [f"skipped {name}: {reason}" for name, reason in scanned.skipped if valid_source_name(name)]
    notes += [f"{src.name}: frontmatter not parsed ({src.fm.error}); listed in the L1 file as is"
              for src in scanned.sources if not src.fm.ok]
    return notes


def _uncovered(scanned: ScanResult, manual: Manual, items: Sequence[Item]) -> list[str]:
    covered = {it.file for it in items if it.origin == "source"}
    missing = [src.name for src in scanned.sources if src.name not in covered]
    if sum(it.origin == "manual" for it in items) != len(manual.lines):
        missing.append(".index-manual.json lines")
    return missing


def _stats(cfg: Config, inputs: Inputs, items: Sequence[Item], rendered: Rendered) -> dict[str, Any]:
    resident = sum(1 for it in items if it.resident and it.origin != "pending")
    return {
        "sources": len(inputs.scanned.sources),
        "skipped": len(inputs.scanned.skipped),
        "manual": len(inputs.data.manual.lines),
        "pending": len(inputs.data.pending),
        "resident": resident,
        "l0_items": resident - len(rendered.overflow),
        "l1_items": sum(1 for it in items if not it.resident and it.origin != "pending"),
        "overflow": len(rendered.overflow),
        "oversized": len(rendered.oversized),
        "unresolved": len(rendered.unresolved),
        "l0_bytes": nbytes(rendered.l0),
        "l0_lines": rendered.l0.count("\n"),
        "budget_bytes": cfg.budget_bytes,
        "budget_lines": cfg.budget_lines,
        "l1_name": cfg.l1_name,
    }


def _read_inputs(sd: StoreDir, cfg: Config, record: Optional[Record], notes: list[str]) -> Inputs:
    stale = stale_generated(sd, record, cfg.l1_name)
    notes.extend(f"stale generated file {name}: delete it (the L1 file is now {cfg.l1_name})" for name in stale)
    scanned = scan(sd, cfg, {L0_NAME, cfg.l1_name, *stale})
    manual, manual_fp = read_manual(sd)
    pending, pending_fp = read_pending(sd)
    return Inputs(scanned, DataFiles(manual, manual_fp, pending, pending_fp), stale)


def _render(inputs: Inputs, cfg: Config, stats: dict[str, Any], notes: list[str]) -> tuple[list[Item], Rendered]:
    data = inputs.data
    items = items_from_sources(inputs.scanned.sources, data.pending, notes) + manual_items(data.manual)
    rendered = render(items, data.manual.preamble, cfg)
    stats.update(_stats(cfg, inputs, items, rendered))
    missing = reachable(items, rendered, data.pending) + _uncovered(inputs.scanned, data.manual, items)
    if missing:
        raise RenderInvariant("not reachable from the index: " + ", ".join(missing[:10]))
    return items, rendered


def refuse_if_migrating(sd: StoreDir, code: int) -> None:
    if sd.kind(MIGRATING_MARK) != "missing":
        raise Refusal(code, f"{MIGRATING_MARK} exists: a migration was interrupted; run `migrate --apply` again")


def prepare(sd: StoreDir, cfg: Config, refuse: int, stats: dict[str, Any], notes: list[str],
            migrating: bool = False) -> Plan:
    """Read the store and render it, writing nothing; the caller decides whether to hold the lock.

    refuse is the exit code for a store that is mid-migration (unless migrate itself asks, with
    migrating) or must be migrated first; problems found on the way are appended to notes."""
    if not migrating:
        refuse_if_migrating(sd, refuse)
    l0, l1 = read_current(sd, L0_NAME), read_current(sd, cfg.l1_name)
    record = read_current(sd, RENDER_MARK, RECORD_MAX_BYTES)
    parsed = read_record(record)
    inputs = _read_inputs(sd, cfg, parsed, notes)
    if needs_migration(l0, record, inputs.scanned.sources, inputs.data.manual_fp is not None):
        raise Refusal(refuse, f"{L0_NAME} was written by hand: run `cc-memory-index migrate` first")
    skipped = skip_notes(inputs.scanned)
    notes += skipped
    items, rendered = _render(inputs, cfg, stats, notes)
    return Plan(inputs, items, rendered, l0, l1, record, parsed, bool(skipped))


def rerender(sd: StoreDir, cfg: Config, plan: Plan, stats: dict[str, Any], notes: list[str]) -> Plan:
    """Re-read sources and data files after the rescue wrote them and render again (DESIGN §6.1 step 4)."""
    inputs = _read_inputs(sd, cfg, plan.parsed, notes)
    skipped = skip_notes(inputs.scanned)
    notes += skipped
    items, rendered = _render(inputs, cfg, stats, notes)
    return dataclasses.replace(plan, inputs=inputs, items=items, rendered=rendered, parse_failed=bool(skipped))
