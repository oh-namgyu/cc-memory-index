"""The build and check commands (DESIGN §4, §7, §8, §10)."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from .generated import (
    MIGRATING_MARK,
    RECORD_MAX_BYTES,
    Current,
    as_text,
    backup_l0,
    edited_generated_files,
    needs_migration,
    parse_record,
    read_current,
    record_bytes,
)
from .items import DataFileError, Item, Manual, items_from_sources, load_manual, load_pending, manual_items
from .render import BudgetError, Rendered, RenderInvariant, nbytes, reachable, render
from .safeio import ConcurrentEdit, LockTimeout, RootChanged, StoreDir, TooLarge, UnsafeEntry
from .store import L0_NAME, RENDER_MARK, Config, ConfigError, ScanResult, load_config, scan, valid_source_name

EXIT_OK = 0
EXIT_OVERFLOW = 1
EXIT_OUT_OF_SYNC = 1
EXIT_USAGE = 2
EXIT_EDITED = 3
EXIT_CONCURRENT = 4
EXIT_LOCK = 5
EXIT_IO = 6
EXIT_PARSE = 7
EXIT_INVARIANT = 8
_FAILURES: tuple[tuple[tuple[type[BaseException], ...], int, str], ...] = (
    ((ConcurrentEdit,), EXIT_CONCURRENT, "edited concurrently, run again: "),
    ((LockTimeout,), EXIT_LOCK, ""),
    ((RenderInvariant,), EXIT_INVARIANT, "internal invariant violated, nothing written: "),
    ((ConfigError, DataFileError, BudgetError, UnsafeEntry, TooLarge), EXIT_USAGE, ""),
    ((OSError, RootChanged), EXIT_IO, "I/O error, the store is still valid; run again: "),
)
_HANDLED: tuple[type[BaseException], ...] = tuple(cls for classes, _, _ in _FAILURES for cls in classes)


@dataclass
class BuildResult:
    code: int
    messages: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


class Refusal(Exception):
    """A command declines to go on; nothing has been written."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _Plan:
    rendered: Rendered
    l0: Current
    l1: Current
    record: Current
    parse_failed: bool


@contextlib.contextmanager
def _outcome(res: BuildResult) -> Iterator[None]:
    """Turn every expected failure into its exit code and one message (DESIGN §10)."""
    try:
        yield
    except Refusal as e:
        res.code = e.code
        res.messages.append(str(e))
    except _HANDLED as e:
        for classes, code, prefix in _FAILURES:
            if isinstance(e, classes):
                res.code = code
                res.messages.append(f"{prefix}{e}")
                return


def _report_skips(scanned: ScanResult, messages: list[str]) -> bool:
    """Report unreadable sources; True when any could not be parsed (exit 7).

    Badly named entries are not memory files and are left to lint (DESIGN §2). A source whose
    frontmatter cannot be parsed is still listed in L1 with its default line, so it stays reachable.
    """
    failed = False
    for name, reason in scanned.skipped:
        if valid_source_name(name):
            messages.append(f"skipped {name}: {reason}")
            failed = True
    for src in scanned.sources:
        if not src.fm.ok:
            messages.append(f"{src.name}: frontmatter not parsed ({src.fm.error}); listed in the L1 file as is")
            failed = True
    return failed


def _uncovered(scanned: ScanResult, manual: Manual, items: Sequence[Item]) -> list[str]:
    covered = {it.file for it in items if it.origin == "source"}
    missing = [src.name for src in scanned.sources if src.name not in covered]
    if sum(it.origin == "manual" for it in items) != len(manual.lines):
        missing.append(".index-manual.json lines")
    return missing


def _stats(cfg: Config, items: Sequence[Item], rendered: Rendered) -> dict[str, Any]:
    resident = sum(1 for it in items if it.resident and it.origin != "pending")
    return {
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


def _prepare(sd: StoreDir, cfg: Config, refuse: int, res: BuildResult) -> _Plan:
    """Read the store and render it; nothing is written. refuse is the exit code for a store
    that must be migrated first."""
    if sd.kind(MIGRATING_MARK) != "missing":
        raise Refusal(refuse, f"{MIGRATING_MARK} exists: a migration was interrupted; run `migrate --apply` again")
    scanned = scan(sd, cfg, {L0_NAME, cfg.l1_name})
    failed = _report_skips(scanned, res.messages)
    manual, pending = load_manual(sd), load_pending(sd)
    l0, l1 = read_current(sd, L0_NAME), read_current(sd, cfg.l1_name)
    record = read_current(sd, RENDER_MARK, RECORD_MAX_BYTES)
    if needs_migration(l0, record, scanned.sources):
        raise Refusal(refuse, f"{L0_NAME} was written by hand: run `cc-memory-index migrate` first")
    items = items_from_sources(scanned.sources, pending, res.messages) + manual_items(manual)
    rendered = render(items, manual.preamble, cfg)
    res.stats.update(sources=len(scanned.sources), skipped=len(scanned.skipped), manual=len(manual.lines),
                     pending=len(pending), **_stats(cfg, items, rendered))
    missing = reachable(items, rendered, pending) + _uncovered(scanned, manual, items)
    if missing:
        raise RenderInvariant("not reachable from the index: " + ", ".join(missing[:10]))
    return _Plan(rendered, l0, l1, record, failed)


def _settle_edits(sd: StoreDir, plan: _Plan, force: bool, res: BuildResult) -> None:
    """Refuse to overwrite hand-edited generated files unless forced (then back up L0 first)."""
    record = parse_record(plan.record)
    if plan.record.data is not None and record is None:
        res.messages.append(f"{RENDER_MARK} is malformed: the generated files are treated as possibly edited")
    current = {"l0": plan.l0, "l1": plan.l1}
    edited = edited_generated_files(record, current, {"l0": plan.rendered.l0, "l1": plan.rendered.l1})
    if not edited:
        return
    names = ", ".join(edited)
    if not force:
        raise Refusal(EXIT_EDITED, (
            f"{names} changed since cc-memory-index last wrote it: put added lines into the memory files'"
            f" frontmatter (idx), or run `build --force` to back up {L0_NAME} and regenerate"
        ))
    note = f"--force: regenerated hand-edited {names}"
    if plan.l0.data is not None:
        res.stats["backup"] = backup_l0(sd, plan.l0.data)
        note += f"; the previous {L0_NAME} is saved as {res.stats['backup']}"
    res.messages.append(note)


def _write(sd: StoreDir, plan: _Plan, res: BuildResult) -> None:
    """L1, then L0, then the render record; each against the fingerprint read (DESIGN §7.2-§7.3)."""
    written: list[str] = []
    res.stats["written"] = written
    for cur, text in ((plan.l1, plan.rendered.l1), (plan.l0, plan.rendered.l0)):
        data = text.encode("utf-8")
        if cur.data != data:
            sd.replace(cur.name, data, cur.fp)
            written.append(cur.name)
    record = record_bytes(plan.rendered.l0, plan.rendered.l1)
    if plan.record.data != record:
        sd.replace(RENDER_MARK, record, plan.record.fp)
        written.append(RENDER_MARK)


def run_build(store_path: str, force: bool) -> BuildResult:
    """Render the store and write the generated files under the store lock."""
    res = BuildResult(EXIT_OK)
    with _outcome(res), StoreDir(store_path) as sd:
        with sd.lock(load_config(sd).lock_timeout_s):
            plan = _prepare(sd, load_config(sd), EXIT_USAGE, res)
            _settle_edits(sd, plan, force, res)
            _write(sd, plan, res)
        if plan.rendered.overflow:
            res.messages.append(
                f"{len(plan.rendered.overflow)} resident entries did not fit the budget and are listed under"
                f" Overflow in {res.stats['l1_name']}: run `cc-memory-index suggest`"
            )
            res.code = EXIT_OVERFLOW
        elif plan.parse_failed:
            res.code = EXIT_PARSE
    return res


def run_check(store_path: str) -> BuildResult:
    """Render without writing and compare with the files on disk (CRLF-normalised); no lock."""
    res = BuildResult(EXIT_OK)
    with _outcome(res), StoreDir(store_path) as sd:
        plan = _prepare(sd, load_config(sd), EXIT_OUT_OF_SYNC, res)
        pairs = ((plan.l0, plan.rendered.l0), (plan.l1, plan.rendered.l1))
        stale = [cur.name for cur, text in pairs if as_text(cur.data) != text]
        res.stats["out_of_sync"] = stale
        if stale:
            res.code = EXIT_OUT_OF_SYNC
        elif plan.parse_failed:
            res.code = EXIT_PARSE
    return res
