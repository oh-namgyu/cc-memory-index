"""The lint command: report problems in a store without changing it (DESIGN §2, §6.2, §10).

Errors exit 1; warnings alone exit 0. The store is read and rendered by plan.prepare, as check
does: nothing is written and the lock is not taken.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .build import EXIT_OK, BuildResult, out_of_sync, outcome
from .fold import folded_text
from .frontmatter import PatchError, locate
from .generated import as_text
from .items import MANUAL_NAME, PENDING_NAME, Item, PendingEntry
from .plan import Plan, Refusal, prepare, skip_notes
from .render import Rendered, nbytes, reachable
from .safeio import StoreDir
from .store import L0_NAME, Config, ScanResult, Source, load_config, valid_source_name
from .text import sanitize

EXIT_ERRORS = 1
_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_WIKI = re.compile(r"\[\[([^\[\]|#\n]+)(?:[|#][^\[\]\n]*)?\]\]")


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


def _check_reachable(plan: Plan, texts: dict[str, Optional[str]], linked: set[str], found: Findings) -> None:
    """A source is reachable when the generated files link to it or list its idx (DESIGN §4)."""
    l0, l1 = (texts[name] or "" for name in texts)
    on_disk = Rendered(l0, l1, [], [], [])
    sources = [it for it in plan.items if it.origin == "source"]
    for name in reachable(sources, on_disk):
        if name not in linked:
            found.errors.append(
                f"{name} is not listed in {' or '.join(texts)}: the index is stale; run `cc-memory-index build`"
            )


def _fold_hint(sd: StoreDir, src: Source, entry: PendingEntry) -> str:
    """Why build has not moved the entry into src (DESIGN §6.1), or that it will."""
    if sd.nlink(src.name) > 1:
        return "build never writes a file with more than one hard link: add the line to its idx by hand"
    try:
        folded_text(src, entry)
    except PatchError as e:
        return f"build cannot add it there ({e}): fix the frontmatter, then run `cc-memory-index build`"
    return "run `cc-memory-index build` to move it there"


def _check_pending(sd: StoreDir, plan: Plan, cfg: Config, found: Findings) -> None:
    """Every pending entry is an error: unresolved (DESIGN §6.2), or not folded into its file yet,
    which a build does unless the fold keeps failing (§6.1)."""
    readable = {src.name: src for src in plan.inputs.scanned.sources}
    for it in plan.items:
        if it.origin != "pending":
            continue
        why = "its memory file has a different idx" if it.file in readable else "its memory file is missing"
        found.errors.append(
            f"{PENDING_NAME}: the line for {it.file} is unresolved ({why}); it is listed under Unresolved in"
            f" {cfg.l1_name}: put the right line into the file's idx, then remove the entry"
        )
    waiting: set[str] = set()
    for entry in plan.inputs.data.pending:
        src = readable.get(entry.file)
        if src is None or entry.file in waiting or sanitize(src.fm.get("idx") or ""):
            continue
        waiting.add(entry.file)
        found.errors.append(f"{PENDING_NAME}: the line for {entry.file} is not in its frontmatter yet;"
                            f" {_fold_hint(sd, src, entry)}")


def _fold(name: str) -> str:
    return name.casefold().replace("_", "-")


def _body(src: Source) -> Optional[str]:
    """The text after the frontmatter; None when the frontmatter cannot be parsed."""
    if not src.fm.present:
        return src.text
    if not src.fm.ok:
        return None
    lines, close = locate(src.text)
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


def _lint_plan(sd: StoreDir, cfg: Config, plan: Plan, notes: list[str], found: Findings) -> None:
    """Every check, then prepare's notes as warnings: stale earlier L1 files, as build reports them,
    and item problems. Skipped sources and unreadable pending files are already errors above."""
    scanned = plan.inputs.scanned
    _check_scan(scanned, found)
    texts = {L0_NAME: as_text(plan.l0.data), cfg.l1_name: as_text(plan.l1.data)}
    linked = _check_links(sd, texts, found)
    _check_reachable(plan, texts, linked, found)
    _check_pending(sd, plan, cfg, found)
    _check_wiki(scanned, found)
    _check_oversized(plan.items, cfg, found)
    found.errors += [f"{name} is out of date: run `cc-memory-index build`" for name in out_of_sync(plan)]
    reported = set(skip_notes(scanned))
    found.warnings += [note for note in notes if note not in reported and not note.startswith(PENDING_NAME)]


def run_lint(store_path: str) -> BuildResult:
    """Report errors and warnings; exit 1 when there is any error. Read-only, no lock."""
    res = BuildResult(EXIT_OK)
    found = Findings()
    notes: list[str] = []
    with outcome(res), StoreDir(store_path) as sd:
        cfg = load_config(sd)
        try:
            plan = prepare(sd, cfg, EXIT_ERRORS, {}, notes)
        except Refusal as e:
            found.errors.append(str(e))
        else:
            res.stats.update(memory_files=len(plan.inputs.scanned.sources), skipped=len(plan.inputs.scanned.skipped))
            _lint_plan(sd, cfg, plan, notes, found)
    res.messages += [f"error: {m}" for m in found.errors] + [f"warning: {m}" for m in found.warnings]
    res.stats.update(errors=len(found.errors), warnings=len(found.warnings))
    if res.code == EXIT_OK and found.errors:
        res.code = EXIT_ERRORS
    return res
