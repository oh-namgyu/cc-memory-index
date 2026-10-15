"""What every command returns, and the exit code of each outcome (DESIGN §10).

A command collects its messages and stats in a BuildResult. outcome() turns every expected
failure into its exit code and one message, so commands do not handle them one by one.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Optional, TypedDict

from .items import DataFileError
from .render import BudgetError, Rendered, RenderInvariant, nbytes
from .safeio import ConcurrentEdit, LockTimeout, RootChanged, StoreDir, TooLarge, UnsafeEntry
from .store import L0_NAME, Config, ConfigError

EXIT_OK = 0
# The command ran and its report needs a look: an overflow (build, suggest), generated files out of
# sync (check), lint errors, or an unfinished migration. Like 0, it is a result, not a failure.
EXIT_ATTENTION = 1
EXIT_USAGE = 2
EXIT_UNRESCUABLE = 3
EXIT_CONCURRENT = 4
EXIT_LOCK = 5
EXIT_IO = 6
EXIT_PARSE = 7
EXIT_INVARIANT = 8


class Refusal(Exception):
    """A command declines to go on; nothing has been written."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


_FAILURES: tuple[tuple[tuple[type[BaseException], ...], int, str], ...] = (
    ((ConcurrentEdit,), EXIT_CONCURRENT, "edited concurrently, run again: "),
    ((LockTimeout,), EXIT_LOCK, ""),
    ((RenderInvariant,), EXIT_INVARIANT, "internal invariant violated, nothing written: "),
    ((ConfigError, DataFileError, BudgetError, UnsafeEntry, TooLarge), EXIT_USAGE, ""),
    ((OSError, RootChanged), EXIT_IO, "I/O error, the store is still valid; run again: "),
)
_HANDLED: tuple[type[BaseException], ...] = tuple(cls for classes, _, _ in _FAILURES for cls in classes)


class Rescued(TypedDict):
    file: str
    idx: str


class Refused(TypedDict):
    segment: str
    reason: str
    kind: str


Converted = Literal["built", "unbuilt"]  # a store that needs no migration: built already, or migrated but not built


class Stats(TypedDict, total=False):
    """The stats of a result, as --json prints them; a command sets only the keys it reports."""

    # The render (plan.prepare); the last six are also a migration's (budget_stats).
    sources: int
    skipped: int
    manual: int
    pending: int
    resident: int
    l0_items: int
    l1_items: int
    oversized: int
    unresolved: int
    l0_bytes: int
    l0_lines: int
    budget_bytes: int
    budget_lines: int
    overflow: int
    l1_name: str
    # build and check
    rescued: list[Rescued]
    manual_added: list[str]
    unrescuable: list[Refused]
    backups: list[str]
    written: list[str]
    out_of_sync: list[str]
    temp_removed: int
    # lint
    memory_files: int
    errors: int
    warnings: int
    # suggest and demote
    transcripts: Optional[int]
    proposal: list[dict[str, Any]]
    l0_bytes_after: int
    l0_lines_after: int
    demoted: list[str]
    build: Stats
    # migrate
    mode: Literal["dry run", "apply", "restore"]
    already: Converted
    snapshot: str
    untouched: int
    notes: int
    notes_file: Optional[str]
    frontmatter_changes: int
    restored: list[str]
    conflicts: list[str]


@dataclass
class BuildResult:
    """A command's exit code, its messages in order and its stats (DESIGN §10)."""

    code: int
    messages: list[str] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)


def new_result() -> BuildResult:
    """The result of a build before it runs: nothing rescued, kept as manual lines or refused yet."""
    return BuildResult(EXIT_OK, stats={"rescued": [], "manual_added": [], "unrescuable": []})


@contextlib.contextmanager
def outcome(res: BuildResult) -> Iterator[None]:
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


@contextlib.contextmanager
def writing(sd: StoreDir, timeout_s: float, stats: Stats) -> Iterator[None]:
    """Hold the store lock for a command that writes, after removing the temporary files that a
    killed write left behind (DESIGN §7.1, §7.4); stats['temp_removed'] counts them."""
    with sd.lock(timeout_s):
        stats["temp_removed"] = sd.sweep_temp()
        yield


def swept(*results: BuildResult) -> int:
    """How many temporary files the commands behind results removed, together."""
    return sum(r.stats.get("temp_removed", 0) for r in results)


def budget_stats(rendered: Rendered, cfg: Config) -> Stats:
    """The size of the rendered L0 against its budget, and how many resident entries overflowed."""
    return {"l0_bytes": nbytes(rendered.l0), "l0_lines": rendered.l0.count("\n"), "budget_bytes": cfg.budget_bytes,
            "budget_lines": cfg.budget_lines, "overflow": len(rendered.overflow), "l1_name": cfg.l1_name}


def budget_line(s: Stats) -> str:
    """The summary line of budget_stats, with the count of unresolved entries when s has one."""
    line = (f"{L0_NAME}: {s['l0_bytes']:,} of {s['budget_bytes']:,} bytes, {s['l0_lines']} of {s['budget_lines']}"
            f" lines; overflow {s['overflow']}")
    return line + (f", unresolved {s['unresolved']}" if "unresolved" in s else "")
