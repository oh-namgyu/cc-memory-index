"""The suggest command: which resident memories to demote so MEMORY.md fits (DESIGN §4.1, §12).

The store is rendered in memory only; with apply=True the proposed memories are demoted and the
store is built.
"""

from __future__ import annotations

import dataclasses
from typing import AbstractSet, Optional, Sequence

from .build import run_build
from .demote import INDEXED, demote_files
from .items import Item
from .plan import prepare
from .render import Rendered, fill, head_lines, item_bytes, nbytes, render, tiers
from .result import EXIT_ATTENTION, EXIT_OK, EXIT_USAGE, BuildResult, outcome, swept
from .safeio import StoreDir
from .store import L0_NAME, Config, load_config
from .transcripts import MAX_TRANSCRIPTS, MIN_TRANSCRIPTS, check_count, count_mentions


@dataclasses.dataclass(frozen=True)
class Candidate:
    file: str
    bytes: int
    mentions: Optional[int]
    oversized: bool


def _among(it: Item, files: AbstractSet[str]) -> bool:
    """True for the item of a memory file in files."""
    return it.origin == "source" and it.file in files


@dataclasses.dataclass(frozen=True)
class _Budget:
    """What fill needs to tell whether a set of resident items fits L0."""

    head: list[str]
    resident: list[Item]
    cfg: Config

    def fits(self, demoted: set[str]) -> bool:
        keep = [it for it in self.resident if not _among(it, demoted)]
        return not fill(self.head, keep, self.cfg).overflow


def _rank(resident: Sequence[Item], cfg: Config, counts: Optional[dict[str, int]]) -> list[Candidate]:
    """Over-cap items first (they never fit), then the least mentioned, then the largest."""
    out: list[Candidate] = []
    for it in resident:
        if it.origin == "source" and it.file is not None:
            out.append(Candidate(it.file, nbytes(it.idx), None if counts is None else counts.get(it.file, 0),
                                 item_bytes(it) > cfg.item_max_bytes))
    return sorted(out, key=lambda c: (not c.oversized, c.mentions or 0, -c.bytes, c.file))


def _shortest_fit(budget: _Budget, ranked: Sequence[Candidate]) -> Optional[int]:
    """Smallest k such that demoting ranked[:k] leaves no overflow (bisection); None if no k does."""
    files = [c.file for c in ranked]
    if not budget.fits(set(files)):
        return None
    low, high = 1, len(files)
    while low < high:
        mid = (low + high) // 2
        if budget.fits(set(files[:mid])):
            high = mid
        else:
            low = mid + 1
    return low


def _describe(c: Candidate, transcripts: Optional[int]) -> str:
    line = f"  {c.file}: {c.bytes} bytes"
    if c.mentions is not None:
        line += f", mentioned in {c.mentions} of the last {transcripts} transcripts"
    if c.oversized:
        line += " (over item_max_bytes, so it cannot be resident: shorten its idx and headings, or demote it)"
    return line


def _verify(items: Sequence[Item], preamble: Optional[str], cfg: Config, chosen: set[str]) -> Rendered:
    """Render again with the chosen memories demoted, in memory only."""
    demoted = [dataclasses.replace(it, resident=False) if _among(it, chosen) else it for it in items]
    return render(demoted, preamble, cfg)


def _propose(store_path: str, items: list[Item], preamble: Optional[str], cfg: Config,
             transcripts: Optional[int], res: BuildResult) -> list[Candidate]:
    resident = tiers(items).resident
    budget = _Budget(head_lines(preamble, cfg.l1_name), resident, cfg)
    overflow = fill(budget.head, resident, cfg).overflow
    res.stats.update({"resident": len(resident), "overflow": len(overflow), "transcripts": transcripts,
                      "proposal": []})
    if not overflow:
        res.messages.append(f"within budget: every resident entry fits {L0_NAME}; nothing to demote")
        return []
    counts = None
    if transcripts is not None:
        names = {it.file for it in resident if it.origin == "source" and it.file is not None}
        counts = count_mentions(store_path, names, transcripts, res.messages)
    ranked = _rank(resident, cfg, counts)
    k = _shortest_fit(budget, ranked)
    rendered = None if k is None else _verify(items, preamble, cfg, {c.file for c in ranked[:k]})
    if k is None or rendered is None or rendered.overflow:
        res.code = EXIT_ATTENTION
        res.messages.append(f"{len(overflow)} entries overflow {L0_NAME} and demoting every resident memory"
                            " would not fix it: shorten the manual lines in .index-manual.json")
        return []
    chosen = list(ranked[:k])
    res.stats.update({"proposal": [dataclasses.asdict(c) for c in chosen], "l0_bytes_after": nbytes(rendered.l0),
                      "l0_lines_after": rendered.l0.count("\n")})
    res.messages.append(f"{len(overflow)} resident entries do not fit {L0_NAME}; demoting these {k} memories"
                        f" (tier: {INDEXED}) makes every other resident entry fit:")
    res.messages += [_describe(c, transcripts) for c in chosen]
    return chosen


def _apply(store_path: str, chosen: Sequence[Candidate], res: BuildResult) -> None:
    """Demote the chosen memories, then build. It builds when nothing is left to demote too, so a
    rerun after the build failed finishes the job (DESIGN §7.2)."""
    demoted = demote_files(store_path, [c.file for c in chosen])
    res.messages += demoted.messages
    res.stats["demoted"] = demoted.stats["demoted"]
    res.stats["temp_removed"] = swept(demoted)
    if demoted.code != EXIT_OK:
        res.code = demoted.code
        return
    built = run_build(store_path, force=False)
    res.code = built.code
    res.messages += built.messages
    res.stats["build"] = built.stats
    res.stats["temp_removed"] = swept(demoted, built)


def run_suggest(store_path: str, transcripts: Optional[int] = None, apply: bool = False) -> BuildResult:
    """Propose resident memories to demote so L0 fits its budget; apply=True demotes them and builds."""
    res = BuildResult(EXIT_OK)
    if transcripts is not None:
        try:
            check_count(transcripts)
        except ValueError:
            return BuildResult(EXIT_USAGE, [f"--transcripts must be between {MIN_TRANSCRIPTS} and {MAX_TRANSCRIPTS}"])
    chosen: list[Candidate] = []
    with outcome(res), StoreDir(store_path) as sd:
        cfg = load_config(sd)
        plan = prepare(sd, cfg, EXIT_USAGE if apply else EXIT_ATTENTION, {}, [])
        chosen = _propose(store_path, plan.items, plan.inputs.data.manual.preamble, cfg, transcripts, res)
    if res.code != EXIT_OK:
        return res
    if apply:
        _apply(store_path, chosen, res)
    elif chosen:
        res.messages.append("run `cc-memory-index suggest --apply` to demote them, or `demote FILE` one at a time")
    return res
