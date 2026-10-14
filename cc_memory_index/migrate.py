"""The migrate command: move a hand-written MEMORY.md into frontmatter (DESIGN §8).

* Dry run (default): plan, render the result in memory and report it; nothing is written.
* --apply, under the store lock: the .index-migrating marker naming a snapshot id, the
  snapshot and its manifest, .index-manual.json, then one memory file at a time. Every original
  segment must then render into L0 or L1; the marker is removed and build writes the index.
  A rerun after any interruption resumes from the snapshot, and every step skips work already
  done, so it converges to the result of an uninterrupted run.
* --restore ID: put back what that migration changed, wherever nothing edited it since.
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator, Optional

from .build import (
    EXIT_CONCURRENT, EXIT_INVARIANT, EXIT_OK, EXIT_OVERFLOW, EXIT_PARSE, EXIT_USAGE, BuildResult, outcome, run_build,
)
from .generated import MIGRATING_MARK, Current, as_text, read_current
from .items import MANUAL_NAME, PENDING_NAME, load_manual, load_pending
from .migrate_plan import Plan, make_plan, manual_bytes, missing_segments, preview, render_store
from .migrate_snapshot import MARKER_MAX_BYTES, SNAPSHOT_PREFIX, Snapshot, marker_ident, new_snapshot_id
from .migrate_snapshot import restore as restore_snapshot
from .plan import Refusal
from .render import Rendered, nbytes
from .safeio import ConcurrentEdit, Fingerprint, StoreDir
from .store import CONFIG_NAME, L0_NAME, RENDER_MARK, Config, ScanResult, load_config, scan

EXIT_INCOMPLETE = 1
RERUN = "run `cc-memory-index migrate --apply` again"
ALREADY = "this store already uses the generated index: nothing to migrate"
UNBUILT = "this store is already migrated (index frontmatter or manual lines), but the index was never built"


def _scan(sd: StoreDir, cfg: Config) -> ScanResult:
    return scan(sd, cfg, {L0_NAME, cfg.l1_name})


def _format_state(sd: StoreDir, scanned: ScanResult) -> Optional[str]:
    """'built', 'unbuilt' or None for a hand-written store.

    'unbuilt' means no render record but a tier or a manual-lines file: migrated already (an index
    that links no memory file migrates into manual lines only) and never built. build makes the
    same distinction (generated.needs_migration), so a rerun after the first build failed builds
    instead of migrating the generated MEMORY.md again.
    """
    if sd.kind(RENDER_MARK) != "missing":
        return "built"
    if sd.kind(MANUAL_NAME) != "missing" or any(src.fm.get("tier") is not None for src in scanned.sources):
        return "unbuilt"
    return None


def _require_all(plan: Plan, rendered: Rendered, code: int, hint: str) -> None:
    missing = missing_segments(plan.segments, rendered)
    if missing:
        shown = "; ".join(repr(text[:80]) for text in missing[:10])
        raise Refusal(code, f"{len(missing)} line(s) of {L0_NAME} would not be in the index ({shown}): {hint}")


def _checked_plan(sd: StoreDir, cfg: Config, scanned: ScanResult, index: Optional[bytes]) -> tuple[Plan, Rendered]:
    """The plan for index (the original MEMORY.md) and its in-memory render; refuses a lossy plan."""
    text = "" if index is None else as_text(index)
    if text is None:
        raise Refusal(EXIT_USAGE, f"{L0_NAME} is not valid UTF-8: convert it before migrating")
    blocked = {src.name for src in scanned.sources if sd.nlink(src.name) > 1}
    load_manual(sd)
    plan = make_plan(text, scanned.sources, blocked)
    rendered = preview(plan, scanned.sources, load_pending(sd), cfg)
    _require_all(plan, rendered, EXIT_INVARIANT, "internal invariant violated, nothing written by this run")
    return plan, rendered


def _describe(plan: Plan, rendered: Rendered, cfg: Config, sources: int) -> dict[str, Any]:
    return {
        "resident": len(plan.resident),
        "manual": len(plan.manual),
        "untouched": sources - len(plan.resident),
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
    scanned = _scan(sd, cfg)
    state = _format_state(sd, scanned)
    if state is not None:
        res.stats["already"] = state
        res.messages.append(ALREADY if state == "built" else f"{UNBUILT}: run `cc-memory-index build`")
        return
    plan, rendered = _checked_plan(sd, cfg, scanned, read_current(sd, L0_NAME).data)
    res.messages.extend(plan.notes)
    res.stats.update(_describe(plan, rendered, cfg, len(scanned.sources)))


def _originals(sd: StoreDir, cfg: Config, scanned: ScanResult, read: tuple[Current, ...]) -> dict[str, bytes]:
    """Every file the migration or its first build may write, as it is now."""
    found = {src.name: src.text.encode("utf-8") for src in scanned.sources}
    others = tuple(read_current(sd, name) for name in (cfg.l1_name, PENDING_NAME, CONFIG_NAME))
    for current in read + others:
        if current.data is not None:
            found[current.name] = current.data
    return found


def _write(sd: StoreDir, cfg: Config, snapshot: Snapshot, scanned: ScanResult) -> Plan:
    """Snapshot and manifest, then .index-manual.json, then one memory file at a time.

    The original MEMORY.md is the snapshot's copy once the manifest exists; until then nothing
    but the marker has been written, so the current file is still the original.
    """
    l0, manual = read_current(sd, L0_NAME), read_current(sd, MANUAL_NAME)
    original = l0 if snapshot.manifest is None else read_current(snapshot.dir, L0_NAME)
    plan, _ = _checked_plan(sd, cfg, scanned, original.data)
    manifest = snapshot.manifest or snapshot.take(_originals(sd, cfg, scanned, (l0, manual)))
    by_name = {src.name: src for src in scanned.sources}
    for name, text in plan.texts.items():
        snapshot.intend(manifest, name, by_name[name].text.encode("utf-8"), text.encode("utf-8"))
    new_manual = manual_bytes(plan)
    snapshot.intend_manual(manifest, manual.data, new_manual)
    snapshot.save(manifest)
    if manual.data != new_manual:
        sd.replace(MANUAL_NAME, new_manual, manual.fp)
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
    state = _format_state(sd, scanned)
    if state is not None:
        res.stats["already"] = state
        res.messages.append(ALREADY if state == "built" else f"{UNBUILT}: building it now")
        return None
    _checked_plan(sd, cfg, scanned, read_current(sd, L0_NAME).data)
    ident = new_snapshot_id(sd)
    return ident, sd.replace(MIGRATING_MARK, f"{ident}\n".encode("ascii"), None)


def _apply(sd: StoreDir, cfg: Config, res: BuildResult) -> bool:
    """--apply up to the removal of the marker; True when build has to run next."""
    res.stats["mode"] = "apply"
    scanned = _scan(sd, cfg)
    started = _start(sd, cfg, scanned, res)
    if started is None:
        return bool(res.stats["already"] == "unbuilt")
    ident, marker_fp = started
    res.stats["snapshot"] = SNAPSHOT_PREFIX + ident
    with sd.subdir(SNAPSHOT_PREFIX + ident, create=True) as snap:
        plan = _write(sd, cfg, Snapshot(snap, ident), scanned)
    res.messages.extend(plan.notes)
    fresh = _scan(sd, cfg)
    rendered = render_store(fresh.sources, load_manual(sd), load_pending(sd), cfg)
    undo = f"{MIGRATING_MARK} is kept; undo with `cc-memory-index migrate --restore {ident}`"
    _require_all(plan, rendered, EXIT_INCOMPLETE, undo)
    res.stats.update(_describe(plan, rendered, cfg, len(fresh.sources)))
    sd.remove(MIGRATING_MARK, marker_fp)
    return True


@contextlib.contextmanager
def _rerun_on_concurrent_edit() -> Iterator[None]:
    try:
        yield
    except ConcurrentEdit as e:
        raise Refusal(EXIT_CONCURRENT, f"edited concurrently ({e}): {RERUN}") from e


def _build(store_path: str, res: BuildResult) -> None:
    """Write the index; an overflow is reported, not a failure of the migration."""
    built = run_build(store_path, False)
    res.messages.extend(built.messages)
    res.stats["build"] = built.stats
    res.code = EXIT_OK if built.code == EXIT_OVERFLOW else built.code
    if built.code not in (EXIT_OK, EXIT_OVERFLOW, EXIT_PARSE):
        res.messages.append(f"the memory files are migrated but the index was not written: fix the above, then {RERUN}")


def run_migrate(store_path: str, apply: bool = False, restore: Optional[str] = None) -> BuildResult:
    """Dry run by default; apply=True migrates; restore (a snapshot id) undoes a migration."""
    res = BuildResult(EXIT_OK)
    if apply and restore is not None:
        return BuildResult(EXIT_USAGE, ["use --apply or --restore, not both"])
    then_build = False
    with outcome(res), StoreDir(store_path) as sd:
        cfg = load_config(sd)
        if restore is not None:
            with sd.lock(cfg.lock_timeout_s):
                restore_snapshot(sd, cfg, restore, res)
        elif apply:
            with sd.lock(cfg.lock_timeout_s), _rerun_on_concurrent_edit():
                then_build = _apply(sd, cfg, res)
        else:
            _dry_run(sd, cfg, res)
    if then_build:
        _build(store_path, res)
    return res


def summary(res: BuildResult) -> list[str]:
    """Human summary of a migrate result for stdout."""
    s = res.stats
    lines: list[str] = []
    if s.get("mode") == "restore":
        lines.append(f"restored {len(s['restored'])} file(s) from {s['snapshot']}; {len(s['conflicts'])} left alone")
    elif "already" in s:
        lines.append(ALREADY if s["already"] == "built" else UNBUILT)
    elif "resident" in s:
        dry = s["mode"] == "dry run"
        lines += [
            f"migrate{' (dry run)' if dry else ''}: {s['resident']} memory files resident, {s['manual']} manual lines,"
            f" {s['untouched']} memory files listed only in {s['l1_name']}",
            f"{L0_NAME}: {s['l0_bytes']:,} of {s['budget_bytes']:,} bytes, {s['l0_lines']} of {s['budget_lines']}"
            f" lines; overflow {s['overflow']}",
            "nothing written: run `cc-memory-index migrate --apply` (it takes a snapshot first)" if dry else
            f"snapshot {s['snapshot']}: undo with `cc-memory-index migrate --restore"
            f" {s['snapshot'][len(SNAPSHOT_PREFIX):]}`",
        ]
    if res.code != EXIT_OK or not lines:
        lines.append(f"migrate: exit {res.code}")
    return lines
