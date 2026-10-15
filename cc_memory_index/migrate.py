"""The migrate command: move a hand-written MEMORY.md into frontmatter (DESIGN §8).

* Dry run (default): plan, render the result in memory and report it; nothing is written.
* --apply, under the store lock: the .index-migrating marker naming a snapshot id, the
  snapshot and its manifest, .index-manual.json, the notes file, then one memory file at a time.
  Every original segment must then render into L0 or L1 and every note be in the notes file. The
  index is built, the notes being known as moved rather than added by hand, and the marker is
  removed last. A rerun after any interruption resumes from the snapshot, and every step skips
  work already done, so it converges to the result of an uninterrupted run.
* --restore ID: put back what that migration changed, wherever nothing edited it since.
"""

from __future__ import annotations

import contextlib
from typing import AbstractSet, Any, Iterator, Optional, Sequence

from .build import (
    EXIT_CONCURRENT, EXIT_INVARIANT, EXIT_OK, EXIT_OVERFLOW, EXIT_PARSE, EXIT_USAGE, BuildResult, build_locked,
    new_result, outcome, writing,
)
from .generated import MIGRATING_MARK, Current, as_text, needs_migration, read_current
from .items import MANUAL_NAME, PENDING_NAME, load_manual, load_pending
from .links import memory_links
from .migrate_notes import first_missing, notes_name
from .migrate_plan import Plan, make_plan, manual_file, missing_segments, preview, render_store
from .migrate_snapshot import MARKER_MAX_BYTES, SNAPSHOT_PREFIX, Snapshot, marker_ident, new_snapshot_id
from .migrate_snapshot import restore as restore_snapshot
from .plan import Refusal
from .render import Rendered, nbytes
from .safeio import ConcurrentEdit, Fingerprint, StoreDir
from .segments import body_lines, pieces
from .store import CONFIG_NAME, L0_NAME, RENDER_MARK, SOURCE_MAX_BYTES, Config, ScanResult, Source, load_config, scan

EXIT_INCOMPLETE = 1
RERUN = "run `cc-memory-index migrate --apply` again"
ALREADY = "this store already uses the generated index: nothing to migrate"
UNBUILT = ("nothing to migrate: MEMORY.md holds no hand-written index (or this store was migrated already),"
           " but the index was never built")


def _format_state(sd: StoreDir) -> Optional[str]:
    """'built', 'unbuilt' or None for a hand-written store (DESIGN §8).

    Only files that this tool writes decide it, the same way build does (generated.needs_migration):
    'built' with a render record; 'unbuilt' with a manual-lines file (every migrate --apply writes
    one), so a rerun after the first build failed builds instead of migrating the generated
    MEMORY.md again, or with a MEMORY.md that holds nothing written by hand. A tier set by hand in
    a memory file is not a sign of a migration.
    """
    if sd.kind(RENDER_MARK) != "missing":
        return "built"
    has_manual = sd.kind(MANUAL_NAME) != "missing"
    return None if needs_migration(read_current(sd, L0_NAME).data, False, has_manual) else "unbuilt"


def _require_all(plan: Plan, rendered: Rendered, notes: Optional[str], code: int, hint: str) -> None:
    """DESIGN §8: every original segment is in L0 or L1, and every note is in notes (the text of the
    notes file), in order."""
    missing = missing_segments(plan.index.segments, rendered)
    if missing:
        shown = "; ".join(repr(text[:80]) for text in missing[:10])
        raise Refusal(code, f"{len(missing)} line(s) of {L0_NAME} would not be in the index ({shown}): {hint}")
    note = first_missing(plan.index.notes.lines, notes)
    if note is not None:
        raise Refusal(code, f"the {L0_NAME} line {note[:80]!r} would not be in {plan.notes_name}: {hint}")


def _notes_name(sd: StoreDir, cfg: Config, text: str, created: Optional[str]) -> str:
    """The notes file the snapshot recorded, else the first free name that the index does not link to."""
    if created is not None:
        return created
    taken = {name.lower() for name in memory_links(text)} | {L0_NAME.lower(), cfg.l1_name.lower()}
    return notes_name(lambda name: name.lower() in taken or sd.kind(name) != "missing")


def _checked_plan(sd: StoreDir, cfg: Config, scanned: ScanResult, index: Optional[bytes],
                  created: Optional[str] = None) -> tuple[Plan, Rendered]:
    """The plan for index (the original MEMORY.md) and its in-memory render; refuses a lossy plan.
    created is the notes file a snapshot already recorded."""
    text = "" if index is None else as_text(index)
    if text is None:
        raise Refusal(EXIT_USAGE, f"{L0_NAME} is not valid UTF-8: convert it before migrating")
    blocked = {src.name for src in scanned.sources if sd.nlink(src.name) > 1}
    plan = make_plan(text, scanned.sources, blocked, _notes_name(sd, cfg, text, created))
    notes = plan.notes_file()
    if notes is not None and len(notes[1].encode("utf-8")) > SOURCE_MAX_BYTES:
        raise Refusal(EXIT_USAGE, f"{L0_NAME} holds more text that is not index lines than a memory file may hold"
                                  f" ({SOURCE_MAX_BYTES // 1024} KiB): move some of it out first")
    rendered = preview(plan, scanned.sources, load_pending(sd), cfg)
    hint = "internal invariant violated, nothing written by this run"
    _require_all(plan, rendered, None if notes is None else notes[1], EXIT_INVARIANT, hint)
    return plan, rendered


def _describe(plan: Plan, rendered: Rendered, cfg: Config, sources: Sequence[Source]) -> dict[str, Any]:
    resident = set(plan.resident)
    return {
        "resident": len(plan.resident),
        "manual": len(plan.manual),
        "untouched": sum(1 for src in sources if src.name not in resident and src.name != plan.notes_name),
        "notes": len(plan.index.notes.lines),
        "notes_file": plan.notes_name,
        "frontmatter_changes": len(plan.texts),
        "l0_bytes": nbytes(rendered.l0),
        "l0_lines": rendered.l0.count("\n"),
        "budget_bytes": cfg.budget_bytes,
        "budget_lines": cfg.budget_lines,
        "overflow": len(rendered.overflow),
        "l1_name": cfg.l1_name,
    }


def _dry_run(sd: StoreDir, cfg: Config, res: BuildResult) -> None:
    res.stats["mode"] = "dry run"
    if sd.kind(MIGRATING_MARK) != "missing":
        raise Refusal(EXIT_INCOMPLETE, f"{MIGRATING_MARK} exists: a migration was interrupted; {RERUN}")
    state = _format_state(sd)
    if state is not None:
        res.stats["already"] = state
        res.messages.append(ALREADY if state == "built" else f"{UNBUILT}: run `cc-memory-index build`")
        return
    scanned = scan(sd, cfg)
    plan, rendered = _checked_plan(sd, cfg, scanned, read_current(sd, L0_NAME).data)
    res.messages.extend(plan.messages)
    res.stats.update(_describe(plan, rendered, cfg, scanned.sources))


def _originals(sd: StoreDir, cfg: Config, scanned: ScanResult, read: tuple[Current, ...]) -> dict[str, bytes]:
    """Every file the migration or its first build may write, as it is now."""
    found = {src.name: src.text.encode("utf-8") for src in scanned.sources}
    others = tuple(read_current(sd, name) for name in (cfg.l1_name, PENDING_NAME, CONFIG_NAME))
    for current in read + others:
        if current.data is not None:
            found[current.name] = current.data
    return found


def _write(sd: StoreDir, cfg: Config, snapshot: Snapshot, scanned: ScanResult) -> Plan:
    """Snapshot and manifest, then .index-manual.json, the notes file, and one memory file at a time.

    The original MEMORY.md is the snapshot's copy once the manifest exists; until then nothing
    but the marker has been written, so the current file is still the original. The notes file is
    created only when it does not exist: a rerun finds the one it wrote, or one edited since.
    """
    l0, manual = read_current(sd, L0_NAME), read_current(sd, MANUAL_NAME)
    original = l0 if snapshot.manifest is None else read_current(snapshot.dir, L0_NAME)
    plan, _ = _checked_plan(sd, cfg, scanned, original.data, snapshot.created())
    manifest = snapshot.manifest or snapshot.take(_originals(sd, cfg, scanned, (l0, manual)))
    by_name = {src.name: src for src in scanned.sources}
    for name, text in plan.texts.items():
        snapshot.intend(manifest, name, by_name[name].text.encode("utf-8"), text.encode("utf-8"))
    notes = plan.notes_file()
    if notes is not None:
        snapshot.intend_new(manifest, notes[0], notes[1].encode("utf-8"))
    new_manual = manual_file(plan)
    snapshot.intend_manual(manifest, manual.data, new_manual)
    snapshot.save(manifest)
    if manual.data != new_manual:
        sd.replace(MANUAL_NAME, new_manual, manual.fp)
    if notes is not None and sd.kind(notes[0]) == "missing":
        sd.replace(notes[0], notes[1].encode("utf-8"), None)
    for name in sorted(plan.texts):
        sd.replace(name, plan.texts[name].encode("utf-8"), by_name[name].fp)
    return plan


def _start(sd: StoreDir, cfg: Config, scanned: ScanResult, res: BuildResult) -> Optional[tuple[str, Fingerprint]]:
    """The snapshot id and marker fingerprint to work with; None when there is nothing to migrate."""
    marker = read_current(sd, MIGRATING_MARK, MARKER_MAX_BYTES)
    if marker.data is not None and marker.fp is not None:
        ident = marker_ident(marker.data)
        if ident is None:
            raise Refusal(EXIT_USAGE, f"{MIGRATING_MARK} does not name a snapshot: remove it, or restore a snapshot")
        res.messages.append(f"resuming the interrupted migration {ident}")
        return ident, marker.fp
    state = _format_state(sd)
    if state is not None:
        res.stats["already"] = state
        res.messages.append(ALREADY if state == "built" else f"{UNBUILT}: building it now")
        return None
    _checked_plan(sd, cfg, scanned, read_current(sd, L0_NAME).data)
    ident = new_snapshot_id(sd)
    return ident, sd.replace(MIGRATING_MARK, f"{ident}\n".encode("ascii"), None)


def _moved(plan: Plan) -> frozenset[str]:
    """The notes as the rescue reads MEMORY.md, in pieces (DESIGN §6): moved, not added by hand."""
    return frozenset(p for line in body_lines(plan.index.notes.lines, False) for p in pieces(line.text))


def _build(sd: StoreDir, cfg: Config, res: BuildResult, moved: Optional[AbstractSet[str]]) -> None:
    """Write the index under the lock held; an overflow is reported, not a failure of the migration."""
    built = new_result()
    with outcome(built):
        build_locked(sd, cfg, built, moved=moved)
    res.messages.extend(built.messages)
    res.stats["build"] = built.stats
    if built.code not in (EXIT_OK, EXIT_OVERFLOW, EXIT_PARSE):
        raise Refusal(built.code, f"the memory files are migrated but the index was not written: fix the above,"
                                  f" then {RERUN}")
    res.code = EXIT_PARSE if built.code == EXIT_PARSE else EXIT_OK


def _apply(sd: StoreDir, cfg: Config, res: BuildResult) -> None:
    """--apply: migrate (or build a store migrated but never built), write the index, remove the marker."""
    res.stats["mode"] = "apply"
    scanned = scan(sd, cfg)
    started = _start(sd, cfg, scanned, res)
    if started is None:
        if res.stats["already"] == "unbuilt":
            _build(sd, cfg, res, None)
        return
    ident, marker_fp = started
    res.stats["snapshot"] = SNAPSHOT_PREFIX + ident
    with sd.subdir(SNAPSHOT_PREFIX + ident, create=True) as snap:
        res.stats["temp_removed"] += snap.sweep_temp()
        plan = _write(sd, cfg, Snapshot(snap, ident), scanned)
    res.messages.extend(plan.messages)
    fresh = scan(sd, cfg)
    rendered = render_store(fresh.sources, load_manual(sd), load_pending(sd), cfg)
    undo = f"{MIGRATING_MARK} is kept; undo with `cc-memory-index migrate --restore {ident}`"
    notes = None if plan.notes_name is None else as_text(read_current(sd, plan.notes_name).data)
    _require_all(plan, rendered, notes, EXIT_INCOMPLETE, undo)
    res.stats.update(_describe(plan, rendered, cfg, fresh.sources))
    _build(sd, cfg, res, _moved(plan))
    sd.remove(MIGRATING_MARK, marker_fp)


@contextlib.contextmanager
def _rerun_on_concurrent_edit() -> Iterator[None]:
    try:
        yield
    except ConcurrentEdit as e:
        raise Refusal(EXIT_CONCURRENT, f"edited concurrently ({e}): {RERUN}") from e


def run_migrate(store_path: str, apply: bool = False, restore: Optional[str] = None) -> BuildResult:
    """Dry run by default; apply=True migrates; restore (a snapshot id) undoes a migration."""
    res = BuildResult(EXIT_OK)
    if apply and restore is not None:
        return BuildResult(EXIT_USAGE, ["use --apply or --restore, not both"])
    with outcome(res), StoreDir(store_path) as sd:
        cfg = load_config(sd)
        if restore is not None:
            with writing(sd, cfg.lock_timeout_s, res.stats):
                restore_snapshot(sd, cfg, restore, res)
        elif apply:
            with writing(sd, cfg.lock_timeout_s, res.stats), _rerun_on_concurrent_edit():
                _apply(sd, cfg, res)
        else:
            _dry_run(sd, cfg, res)
    return res


def summary(res: BuildResult) -> list[str]:
    """Human summary of a migrate result, after its messages (which it does not repeat); the caller
    adds the exit line."""
    s = res.stats
    lines: list[str] = []
    if s.get("mode") == "restore":
        lines.append(f"restored {len(s['restored'])} file(s) from {s['snapshot']}; {len(s['conflicts'])} left alone")
    elif "resident" in s:
        dry = s["mode"] == "dry run"
        lines += [
            f"migrate{' (dry run)' if dry else ''}: {s['resident']} memory files resident, {s['manual']} manual lines,"
            f" {s['untouched']} memory files listed only in {s['l1_name']}",
            f"{L0_NAME}: {s['l0_bytes']:,} of {s['budget_bytes']:,} bytes, {s['l0_lines']} of {s['budget_lines']}"
            f" lines; overflow {s['overflow']}",
        ]
        if s["notes"]:
            lines.append(f"{s['notes']} line(s) that are not index lines (code blocks, nested lists, paragraphs)"
                         f" {'would move' if dry else 'moved'} verbatim to {s['notes_file']}")
        lines += [
            "nothing written: run `cc-memory-index migrate --apply` (it takes a snapshot first)" if dry else
            f"snapshot {s['snapshot']}: undo with `cc-memory-index migrate --restore"
            f" {s['snapshot'][len(SNAPSHOT_PREFIX):]}`",
        ]
    return lines
