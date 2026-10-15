"""The build and check commands (DESIGN §4, §6, §7, §8, §10)."""

from __future__ import annotations

from typing import AbstractSet, Optional, Sequence

from .fold import Folded, fold
from .generated import Current, Role, as_text, backup_generated, edited_generated_files, known_lines, record_bytes
from .items import MANUAL_NAME, PENDING_NAME
from .plan import Plan, prepare, rerender
from .render import RenderInvariant
from .rescue import Context, Edited, Rescue, analyse, body_pieces, default_pieces, lost
from .result import (
    EXIT_ATTENTION, EXIT_CONCURRENT, EXIT_OK, EXIT_PARSE, EXIT_UNRESCUABLE, EXIT_USAGE, BuildResult, Refusal,
    new_result, outcome, writing,
)
from .safeio import StoreDir
from .segments import pieces
from .store import RENDER_MARK, Config, load_config
from .text import quote


def _edited(plan: Plan, res: BuildResult) -> list[Edited]:
    """The generated files whose content differs from what this tool wrote (DESIGN §6)."""
    if plan.record.data is not None and plan.parsed is None:
        res.messages.append(f"{RENDER_MARK} is malformed: the generated files are treated as possibly edited")
    current: dict[Role, Current] = {"l0": plan.l0, "l1": plan.l1}
    fresh: dict[Role, str] = {"l0": plan.rendered.l0, "l1": plan.rendered.l1}
    roles = edited_generated_files(plan.parsed, current, fresh, known_lines(plan.inputs.data.manual.preamble))
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


def _context(plan: Plan, cfg: Config, moved: AbstractSet[str]) -> Context:
    data = plan.inputs.data
    recorded = plan.parsed.l1_name if plan.parsed is not None else cfg.l1_name
    names = (cfg.l1_name,) if recorded == cfg.l1_name else (cfg.l1_name, recorded)
    sources = plan.inputs.scanned.sources
    max_order = max((it.order for it in plan.items), default=0)
    return Context(plan.rendered, data.manual.preamble, names, {src.name: src for src in sources},
                   frozenset(e.file for e in data.pending), max_order, default_pieces(sources) | moved)


def _rescue(plan: Plan, cfg: Config, edited: Sequence[Edited], moved: AbstractSet[str], res: BuildResult) -> Rescue:
    """Classify the lines added by hand; refuse (exit 3, nothing written) when any cannot be kept."""
    found = analyse(edited, _context(plan, cfg, moved))
    res.stats["unrescuable"] = [{"segment": u.segment, "reason": u.reason, "kind": u.kind} for u in found.unrescuable]
    if found.unrescuable:
        for u in found.unrescuable:
            where = f"{u.file}: {quote(u.segment, 100)}" if u.segment else u.file
            res.messages.append(f"{where} — {u.reason}")
        names = ", ".join(ed.name for ed in edited)
        raise Refusal(EXIT_UNRESCUABLE, (
            f"nothing written: {len(found.unrescuable)} hand edit(s) of {names} cannot be kept; fix them as"
            " described above, or run `build --force` to back up the generated files and regenerate them"
        ))
    res.stats["rescued"] = [{"file": e.file, "idx": e.idx} for e in found.entries]
    res.stats["manual_added"] = [line.seg for line in found.lines]
    res.messages.extend(f"moved a line added by hand into {e.file} (metadata.idx)" for e in found.entries)
    res.messages.extend(f"kept a line added by hand as a manual line ({MANUAL_NAME}): {quote(line.seg)}"
                        for line in found.lines)
    return found


def _check_kept(old: Plan, new: Plan, cfg: Config, rescue: Rescue, edited: Sequence[Edited], force: bool,
                moved: AbstractSet[str]) -> None:
    """DESIGN §6 invariant: no line of the current L0 and L1 is lost, or they are not replaced (exit 8).

    Default lines, the lines of sources that changed during the run (patched, or edited
    concurrently) and moved lines may go; files given up with --force are not checked.
    """
    now = {src.name: src.fp for src in new.inputs.scanned.sources}
    changed = {src.name for src in old.inputs.scanned.sources if now.get(src.name) != src.fp}
    generated = {p for it in old.items if it.file in changed for p in pieces(it.idx)}
    generated |= default_pieces(old.inputs.scanned.sources) | moved
    roles = {ed.role for ed in edited}
    files: tuple[tuple[Role, Current], ...] = (("l0", old.l0), ("l1", old.l1))
    current = [(as_text(cur.data), rescue.pieces.get(role) if role in roles else None)
               for role, cur in files if not (force and role in roles)]
    fresh = body_pieces(old.rendered, old.inputs.data.manual.preamble, cfg.l1_name)
    after = body_pieces(new.rendered, new.inputs.data.manual.preamble, cfg.l1_name)
    missing = lost(current, fresh, after, generated)
    if missing:
        raise RenderInvariant(
            f"{len(missing)} line(s) of the generated files would be lost (first: {quote(missing[0])});"
            " they were not rewritten"
        )


def _settle(sd: StoreDir, cfg: Config, plan: Plan, force: bool, moved: AbstractSet[str],
            res: BuildResult) -> tuple[Plan, Folded]:
    """Keep hand edits (DESIGN §6) or back them up (--force), fold pending lines (§6.1), and render
    again from fresh reads when anything was written."""
    edited = _edited(plan, res)
    rescue = Rescue()
    if edited and force:
        _force(sd, plan, edited, res)
    elif edited:
        rescue = _rescue(plan, cfg, edited, moved, res)
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
        _check_kept(plan, new, cfg, rescue, edited, force, moved)
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
        res.code = EXIT_ATTENTION
    elif plan.parse_failed:
        res.code = EXIT_PARSE


def build_locked(sd: StoreDir, cfg: Config, res: BuildResult, force: bool = False,
                 moved: Optional[AbstractSet[str]] = None) -> None:
    """Render the store and write the generated files; the caller holds the store lock.

    moved is set only by migrate, before it removes its marker: the pieces of the lines it moved
    from MEMORY.md to the notes file, which may leave MEMORY.md without being taken for hand edits.
    """
    plan = prepare(sd, cfg, EXIT_USAGE, res.stats, res.messages, migrating=moved is not None)
    plan, folded = _settle(sd, cfg, plan, force, moved or frozenset(), res)
    _write(sd, cfg, plan, res)
    _finish(plan, folded, res)


def run_build(store_path: str, force: bool) -> BuildResult:
    """Render the store and write the generated files under the store lock."""
    res = new_result()
    with outcome(res), StoreDir(store_path) as sd:
        cfg = load_config(sd)
        with writing(sd, cfg.lock_timeout_s, res.stats):
            build_locked(sd, cfg, res, force)
    return res


def out_of_sync(plan: Plan) -> list[str]:
    """The generated files whose content (CRLF-normalised) differs from the fresh render."""
    pairs = ((plan.l0, plan.rendered.l0), (plan.l1, plan.rendered.l1))
    return [cur.name for cur, text in pairs if as_text(cur.data) != text]


def run_check(store_path: str) -> BuildResult:
    """Render without writing and compare with the files on disk; no lock."""
    res = BuildResult(EXIT_OK)
    with outcome(res), StoreDir(store_path) as sd:
        plan = prepare(sd, load_config(sd), EXIT_ATTENTION, res.stats, res.messages)
        stale = out_of_sync(plan)
        res.stats["out_of_sync"] = stale
        if stale:
            res.code = EXIT_ATTENTION
        elif plan.parse_failed:
            res.code = EXIT_PARSE
    return res
