"""The build and check commands (DESIGN §4, §6, §7, §8, §10)."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from .fold import Folded, fold
from .generated import as_text, backup_generated, edited_generated_files, record_bytes
from .items import MANUAL_NAME, PENDING_NAME, DataFileError
from .plan import Plan, Refusal, prepare, rerender
from .render import BudgetError, RenderInvariant
from .rescue import Context, Edited, Rescue, analyse, body_pieces, default_pieces, lost
from .safeio import ConcurrentEdit, LockTimeout, RootChanged, StoreDir, TooLarge, UnsafeEntry
from .segments import pieces
from .store import RENDER_MARK, Config, ConfigError, load_config

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


def _edited(plan: Plan, res: BuildResult) -> list[Edited]:
    """The generated files whose content differs from what this tool wrote (DESIGN §6)."""
    if plan.record.data is not None and plan.parsed is None:
        res.messages.append(f"{RENDER_MARK} is malformed: the generated files are treated as possibly edited")
    current = {"l0": plan.l0, "l1": plan.l1}
    fresh = {"l0": plan.rendered.l0, "l1": plan.rendered.l1}
    roles = edited_generated_files(plan.parsed, current, fresh)
    return [Edited(current[role].name, role, as_text(current[role].data)) for role in roles]


def _force(sd: StoreDir, plan: Plan, edited: Sequence[Edited], res: BuildResult) -> None:
    """build --force: back up every generated file that differs from the fresh render, skip the rescue."""
    backups: list[str] = []
    for cur, text in ((plan.l0, plan.rendered.l0), (plan.l1, plan.rendered.l1)):
        if cur.data is not None and as_text(cur.data) != text:
            backups.append(backup_generated(sd, cur.name, cur.data))
    res.stats["backups"] = backups
    names = ", ".join(ed.name for ed in edited)
    res.messages.append(f"--force: regenerated hand-edited {names}; previous content saved as {', '.join(backups)}")


def _context(plan: Plan, cfg: Config) -> Context:
    data = plan.inputs.data
    recorded = plan.parsed.l1_name if plan.parsed is not None else cfg.l1_name
    names = (cfg.l1_name,) if recorded == cfg.l1_name else (cfg.l1_name, recorded)
    sources = plan.inputs.scanned.sources
    max_order = max((it.order for it in plan.items), default=0)
    return Context(plan.rendered, data.manual.preamble, names, {src.name: src for src in sources},
                   frozenset(e.file for e in data.pending), max_order, default_pieces(sources))


def _rescue(plan: Plan, cfg: Config, edited: Sequence[Edited], res: BuildResult) -> Rescue:
    """Classify the lines added by hand; refuse (exit 3, nothing written) when any cannot be kept."""
    found = analyse(edited, _context(plan, cfg))
    res.stats["unrescuable"] = [{"segment": u.segment, "reason": u.reason} for u in found.unrescuable]
    if found.unrescuable:
        for u in found.unrescuable:
            where = f"{u.file}: {u.segment[:100]!r}" if u.segment else u.file
            res.messages.append(f"{where} — {u.reason}")
        names = ", ".join(ed.name for ed in edited)
        raise Refusal(EXIT_EDITED, (
            f"nothing written: {len(found.unrescuable)} hand edit(s) of {names} cannot be kept; fix them as"
            " described above, or run `build --force` to back up the generated files and regenerate them"
        ))
    res.stats["rescued"] = [{"file": e.file, "idx": e.idx} for e in found.entries]
    res.stats["manual_added"] = [line.seg for line in found.lines]
    res.messages.extend(f"moved a line added by hand into {e.file} (metadata.idx)" for e in found.entries)
    res.messages.extend(f"kept a line added by hand as a manual line ({MANUAL_NAME}): {line.seg[:80]!r}"
                        for line in found.lines)
    return found


def _check_kept(old: Plan, new: Plan, cfg: Config, rescue: Rescue, edited: Sequence[Edited], force: bool) -> None:
    """DESIGN §6 invariant: no line of the current L0 and L1 is lost, or they are not replaced (exit 8).

    Default lines and the lines of sources that changed during the run (patched, or edited
    concurrently) may go; files given up with --force are not checked.
    """
    now = {src.name: src.fp for src in new.inputs.scanned.sources}
    changed = {src.name for src in old.inputs.scanned.sources if now.get(src.name) != src.fp}
    generated = {p for it in old.items if it.file in changed for p in pieces(it.idx)}
    generated |= default_pieces(old.inputs.scanned.sources)
    roles = {ed.role for ed in edited}
    current = [(as_text(cur.data), rescue.pieces.get(role) if role in roles else None)
               for role, cur in (("l0", old.l0), ("l1", old.l1)) if not (force and role in roles)]
    fresh = body_pieces(old.rendered, old.inputs.data.manual.preamble, cfg.l1_name)
    after = body_pieces(new.rendered, new.inputs.data.manual.preamble, cfg.l1_name)
    missing = lost(current, fresh, after, generated)
    if missing:
        raise RenderInvariant(
            f"{len(missing)} line(s) of the generated files would be lost (first: {missing[0][:80]!r});"
            " they were not rewritten"
        )


def _settle(sd: StoreDir, cfg: Config, plan: Plan, force: bool, res: BuildResult) -> tuple[Plan, Folded]:
    """Keep hand edits (DESIGN §6) or back them up (--force), fold pending lines (§6.1), and render
    again from fresh reads when anything was written."""
    edited = _edited(plan, res)
    rescue = Rescue()
    if edited and force:
        _force(sd, plan, edited, res)
    elif edited:
        rescue = _rescue(plan, cfg, edited, res)
    folded = Folded()
    res.stats["written"] = folded.written
    fold(sd, plan.inputs.data, plan.inputs.scanned.sources, rescue.entries, rescue.lines, folded)
    res.messages.extend(folded.messages)
    new = plan
    if folded.written:
        notes: list[str] = []
        new = rerender(sd, cfg, plan, res.stats, notes)
        res.messages.extend(note for note in notes if note not in res.messages)
    if edited or folded.written:
        _check_kept(plan, new, cfg, rescue, edited, force)
    return new, folded


def _write(sd: StoreDir, cfg: Config, plan: Plan, res: BuildResult) -> None:
    """L1, then L0, then the render record; each against the fingerprint read (DESIGN §7.2-§7.3)."""
    written: list[str] = res.stats["written"]
    for cur, text in ((plan.l1, plan.rendered.l1), (plan.l0, plan.rendered.l0)):
        data = text.encode("utf-8")
        if cur.data != data:
            sd.replace(cur.name, data, cur.fp)
            written.append(cur.name)
    record = record_bytes(plan.rendered.l0, plan.rendered.l1, cfg.l1_name, plan.inputs.stale)
    if plan.record.data != record:
        sd.replace(RENDER_MARK, record, plan.record.fp)
        written.append(RENDER_MARK)


def _finish(plan: Plan, folded: Folded, res: BuildResult) -> None:
    if plan.rendered.overflow:
        res.messages.append(
            f"{len(plan.rendered.overflow)} resident entries did not fit the budget and are listed under"
            f" Overflow in {res.stats['l1_name']}: run `cc-memory-index suggest`"
        )
    if folded.concurrent:
        res.code = EXIT_CONCURRENT
        res.messages.append(
            f"edited concurrently, run again: {', '.join(folded.concurrent)} changed during the build, so its"
            f" rescued line stays in {PENDING_NAME} for now"
        )
    elif plan.rendered.overflow:
        res.code = EXIT_OVERFLOW
    elif plan.parse_failed:
        res.code = EXIT_PARSE


def run_build(store_path: str, force: bool) -> BuildResult:
    """Render the store and write the generated files under the store lock."""
    res = BuildResult(EXIT_OK, stats={"rescued": [], "manual_added": [], "unrescuable": []})
    with _outcome(res), StoreDir(store_path) as sd:
        with sd.lock(load_config(sd).lock_timeout_s):
            cfg = load_config(sd)
            plan = prepare(sd, cfg, EXIT_USAGE, res.stats, res.messages)
            plan, folded = _settle(sd, cfg, plan, force, res)
            _write(sd, cfg, plan, res)
        _finish(plan, folded, res)
    return res


def run_check(store_path: str) -> BuildResult:
    """Render without writing and compare with the files on disk (CRLF-normalised); no lock."""
    res = BuildResult(EXIT_OK)
    with _outcome(res), StoreDir(store_path) as sd:
        plan = prepare(sd, load_config(sd), EXIT_OUT_OF_SYNC, res.stats, res.messages)
        pairs = ((plan.l0, plan.rendered.l0), (plan.l1, plan.rendered.l1))
        stale = [cur.name for cur, text in pairs if as_text(cur.data) != text]
        res.stats["out_of_sync"] = stale
        if stale:
            res.code = EXIT_OUT_OF_SYNC
        elif plan.parse_failed:
            res.code = EXIT_PARSE
    return res
