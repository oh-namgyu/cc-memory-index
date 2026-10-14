"""The lint command: report problems in a store without changing it (DESIGN §2, §6.2, §10).

Errors exit 1; warnings alone exit 0. Nothing is written and the lock is not taken.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from .build import EXIT_OK, EXIT_PARSE, BuildResult, Refusal, _outcome, run_check
from .frontmatter import _locate
from .generated import MIGRATING_MARK, RECORD_MAX_BYTES, Current, as_text, needs_migration, read_current
from .items import (
    MANUAL_NAME, PENDING_NAME, Item, Manual, PendingEntry, items_from_sources, load_manual, load_pending, manual_items,
)
from .render import Rendered, nbytes, reachable
from .safeio import StoreDir
from .store import L0_NAME, RENDER_MARK, Config, ScanResult, Source, load_config, scan, valid_source_name

EXIT_ERRORS = 1
_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_WIKI = re.compile(r"\[\[([^\[\]|#\n]+)(?:[|#][^\[\]\n]*)?\]\]")


@dataclass
class View:
    """Everything a read-only command needs from a store, read once without the lock."""

    scanned: ScanResult
    manual: Manual
    pending: list[PendingEntry]
    items: list[Item]
    l0: Current
    l1: Current
    record: Current


def refuse_if_migrating(sd: StoreDir, code: int) -> None:
    if sd.kind(MIGRATING_MARK) != "missing":
        raise Refusal(code, f"{MIGRATING_MARK} exists: a migration was interrupted; run `migrate --apply` again")


def read_view(sd: StoreDir, cfg: Config, refuse: int, notes: list[str]) -> View:
    """Sources, data files, generated files and items; refuse is the exit code for a store that
    is mid-migration or must be migrated first. Item problems are appended to notes."""
    refuse_if_migrating(sd, refuse)
    scanned = scan(sd, cfg, {L0_NAME, cfg.l1_name})
    manual, pending = load_manual(sd), load_pending(sd)
    l0, l1 = read_current(sd, L0_NAME), read_current(sd, cfg.l1_name)
    record = read_current(sd, RENDER_MARK, RECORD_MAX_BYTES)
    if needs_migration(l0, record, scanned.sources, sd.kind(MANUAL_NAME) != "missing"):
        raise Refusal(refuse, f"{L0_NAME} was written by hand: run `cc-memory-index migrate` first")
    items = items_from_sources(scanned.sources, pending, notes) + manual_items(manual)
    return View(scanned, manual, pending, items, l0, l1, record)


@dataclass
class Findings:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _check_scan(scanned: ScanResult, found: Findings) -> None:
    for name, reason in scanned.skipped:
        if valid_source_name(name):
            found.errors.append(f"{name} was skipped: {reason}")
        else:
            found.warnings.append(f"{name!r} is not a valid memory file name, so it is not indexed")
    for src in scanned.sources:
        named = src.fm.get("name")
        if not src.fm.ok:
            found.errors.append(f"{src.name}: frontmatter cannot be parsed ({src.fm.error})")
        elif not src.fm.present:
            found.warnings.append(f"{src.name} has no frontmatter: it is indexed by its file name only")
        elif named is not None and named != src.name[: -len(".md")]:
            found.warnings.append(f"{src.name}: frontmatter name {named!r} differs from the file name")


def _link_targets(text: str) -> list[str]:
    targets: list[str] = []
    for m in _LINK.finditer(text):
        target = m.group(1).split("#", 1)[0]
        if target.endswith(".md") and not _SCHEME.match(target) and target not in targets:
            targets.append(target)
    return targets


def _check_links(sd: StoreDir, texts: dict[str, Optional[str]], found: Findings) -> set[str]:
    """Report dangling .md links in the generated files; returns the plain names linked."""
    linked: set[str] = set()
    for generated, text in texts.items():
        for target in _link_targets(text or ""):
            if not valid_source_name(target):
                found.warnings.append(f"{generated} links to {target!r}, which is not a file in the store")
                continue
            linked.add(target)
            if sd.kind(target) == "missing":
                found.errors.append(f"{generated} links to {target}, which does not exist")
    return linked


def _check_reachable(view: View, texts: dict[str, Optional[str]], linked: set[str], found: Findings) -> None:
    """A source is reachable when the generated files link to it or list its idx (DESIGN §4)."""
    l0, l1 = (texts[name] or "" for name in texts)
    on_disk = Rendered(l0, l1, [], [], [])
    sources = [it for it in view.items if it.origin == "source"]
    for name in reachable(sources, on_disk):
        if name not in linked:
            found.errors.append(
                f"{name} is not listed in {' or '.join(texts)}: the index is stale; run `cc-memory-index build`"
            )


def _check_pending(view: View, cfg: Config, found: Findings) -> None:
    readable = {src.name for src in view.scanned.sources}
    for it in view.items:
        if it.origin != "pending":
            continue
        why = "its memory file has a different idx" if it.file in readable else "its memory file is missing"
        found.errors.append(
            f"{PENDING_NAME}: the line for {it.file} is unresolved ({why}); it is listed under Unresolved in"
            f" {cfg.l1_name}: put the right line into the file's idx, then remove the entry"
        )


def _fold(name: str) -> str:
    return name.casefold().replace("_", "-")


def _body(src: Source) -> Optional[str]:
    """The text after the frontmatter; None when the frontmatter cannot be parsed."""
    if not src.fm.present:
        return src.text
    if not src.fm.ok:
        return None
    lines, close = _locate(src.text)
    return src.text[len("\n".join(lines[: close + 1])):]


def _wiki_names(scanned: ScanResult) -> tuple[set[str], dict[str, str]]:
    """Exact wiki-link names (file stems and frontmatter names) and their loose forms."""
    exact: dict[str, str] = {}
    for name in [src.name for src in scanned.sources] + [n for n, _ in scanned.skipped if valid_source_name(n)]:
        exact.setdefault(name[: -len(".md")], name)
    for src in scanned.sources:
        named = src.fm.get("name")
        if named:
            exact.setdefault(named, src.name)
    loose: dict[str, str] = {}
    for key, name in sorted(exact.items()):
        loose.setdefault(_fold(key), name)
    return set(exact), loose


def _check_wiki(scanned: ScanResult, found: Findings) -> None:
    exact, loose = _wiki_names(scanned)
    for src in scanned.sources:
        seen: set[str] = set()
        for m in _WIKI.finditer(_body(src) or ""):
            target = m.group(1).strip()
            stem = target[: -len(".md")] if target.endswith(".md") else target
            if not stem or target in seen or target in exact or stem in exact:
                continue
            seen.add(target)
            if _fold(stem) in loose:
                found.warnings.append(
                    f"{src.name}: wiki link {target!r} matches {loose[_fold(stem)]} only when case and '-'/'_'"
                    " are ignored"
                )
            else:
                found.errors.append(f"{src.name}: wiki link {target!r} does not resolve to any memory file")


def _check_oversized(items: list[Item], cfg: Config, found: Findings) -> None:
    for it in items:
        size = nbytes(it.idx)
        if it.resident and it.origin != "pending" and size > cfg.item_max_bytes:
            label = it.file or f"{it.idx[:40]!r} in {MANUAL_NAME}"
            found.warnings.append(
                f"{label}: idx is {size} bytes, over item_max_bytes ({cfg.item_max_bytes}), so it is listed under"
                f" Overflow in {cfg.l1_name}: shorten it or demote the memory"
            )


def _check_previous_l1(sd: StoreDir, cfg: Config, record: Current, found: Findings) -> None:
    """The render record may name the L1 file written before l1_name was changed."""
    try:
        obj = json.loads(as_text(record.data) or "null")
    except (ValueError, RecursionError):
        return
    previous = obj.get("l1_name") if isinstance(obj, dict) else None
    if not isinstance(previous, str) or not valid_source_name(previous):
        return
    if previous.lower() != cfg.l1_name.lower() and sd.kind(previous) != "missing":
        found.warnings.append(
            f"{previous} is the previous L1 file (l1_name is now {cfg.l1_name}): delete it once nothing links to it"
        )


def _check_sync(store_path: str, found: Findings) -> None:
    checked = run_check(store_path)
    stale = checked.stats.get("out_of_sync")
    if stale:
        found.errors += [f"{name} is out of date: run `cc-memory-index build`" for name in stale]
    elif stale is None and checked.code not in (EXIT_OK, EXIT_PARSE) and checked.messages:
        found.errors.append(f"check failed (exit {checked.code}): {checked.messages[-1]}")


def _lint_view(sd: StoreDir, cfg: Config, view: View, notes: list[str], found: Findings) -> None:
    _check_scan(view.scanned, found)
    texts = {L0_NAME: as_text(view.l0.data), cfg.l1_name: as_text(view.l1.data)}
    linked = _check_links(sd, texts, found)
    _check_reachable(view, texts, linked, found)
    _check_pending(view, cfg, found)
    _check_wiki(view.scanned, found)
    _check_oversized(view.items, cfg, found)
    _check_previous_l1(sd, cfg, view.record, found)
    found.warnings += [note for note in notes if not note.startswith(PENDING_NAME)]


def run_lint(store_path: str) -> BuildResult:
    """Report errors and warnings; exit 1 when there is any error. Read-only, no lock."""
    res = BuildResult(EXIT_OK)
    found = Findings()
    notes: list[str] = []
    with _outcome(res), StoreDir(store_path) as sd:
        cfg = load_config(sd)
        try:
            view = read_view(sd, cfg, EXIT_ERRORS, notes)
        except Refusal as e:
            found.errors.append(str(e))
        else:
            res.stats.update(memory_files=len(view.scanned.sources), skipped=len(view.scanned.skipped))
            _lint_view(sd, cfg, view, notes, found)
            _check_sync(store_path, found)
    res.messages += [f"error: {m}" for m in found.errors] + [f"warning: {m}" for m in found.warnings]
    res.stats.update(errors=len(found.errors), warnings=len(found.warnings))
    if res.code == EXIT_OK and found.errors:
        res.code = EXIT_ERRORS
    return res
