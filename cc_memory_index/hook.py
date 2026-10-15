"""PostToolUse hook entry point (DESIGN §11).

Claude Code runs this after every Write, Edit or MultiEdit. It rebuilds the index only for a
memory file that sits directly inside a store this tool has already written, and tells Claude
what happened through `additionalContext`. That text is fixed wording, counts and validated
memory file names; nothing else read from the store reaches Claude through it. It always exits
0: a bug here must never block a session.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import IO, Mapping, Optional

from .build import run_build
from .result import (
    EXIT_ATTENTION, EXIT_CONCURRENT, EXIT_INVARIANT, EXIT_IO, EXIT_LOCK, EXIT_OK, EXIT_PARSE, EXIT_UNRESCUABLE,
    EXIT_USAGE, BuildResult, Stats,
)
from .store import store_root_for_edit, valid_source_name

MAX_INPUT_BYTES = 1 << 20
MAX_CONTEXT_CHARS = 2000
MAX_NAMES = 5
TOOLS = frozenset({"Write", "Edit", "MultiEdit"})
ENV_DISABLE = "CC_MEMORY_INDEX_DISABLE"
PREFIX = "cc-memory-index: "
SEE_LINT = "Run `cc-memory-index lint` to see the details."
NOT_REBUILT = {
    EXIT_USAGE: "The index was not rebuilt: the store's configuration or one of its data files is invalid,"
                " or a migration is unfinished.",
    EXIT_IO: "The index was not rebuilt because of an I/O error; the store is still valid.",
    EXIT_INVARIANT: "The index was not rebuilt: an internal consistency check failed, so nothing was written.",
}


def target_store(raw: bytes) -> Optional[str]:
    """The store to rebuild for this hook payload, or None when the call is not ours."""
    if len(raw) > MAX_INPUT_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):  # RecursionError: deeply nested JSON
        return None
    if not isinstance(payload, dict) or payload.get("tool_name") not in TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    return store_root_for_edit(file_path) if isinstance(file_path, str) else None


def _count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _rescued(stats: Stats) -> Optional[str]:
    entries = stats.get("rescued")
    files = [e.get("file") for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    names: list[str] = [name for name in files if isinstance(name, str) and valid_source_name(name)]
    if not names:
        return None
    shown = ", ".join(names[:MAX_NAMES]) + (f" and {len(names) - MAX_NAMES} more" if len(names) > MAX_NAMES else "")
    saved = (f"The line you added for {shown} was saved as that file's" if len(names) == 1
             else f"The lines you added for {shown} were saved as those files'")
    return (
        f"MEMORY.md is generated from each memory file's frontmatter. {saved} metadata.idx and listed in the"
        " on-demand index, which is the right place for most memories. Set metadata.tier: resident only if"
        " a memory must be known at the start of every session; MEMORY.md has a size budget."
    )


def _outcome(code: int, stats: Stats) -> Optional[str]:
    """One fixed sentence for the build's exit code (DESIGN §10), or None when there is nothing to say."""
    if code in (EXIT_OK, EXIT_LOCK):
        return None
    if code == EXIT_ATTENTION:  # build exits 1 only for an overflow
        overflow = stats.get("overflow")
        count = f"{overflow} resident entries" if isinstance(overflow, int) else "Some resident entries"
        return (f"{count} no longer fit the MEMORY.md budget and are listed in the on-demand index instead."
                " Run `cc-memory-index suggest` to choose memories to demote.")
    if code == EXIT_UNRESCUABLE:
        return (
            f"Your edit to the generated index was not applied: {_count(stats.get('unrescuable'))} added line(s)"
            " could not be kept. A line added by hand is kept only when it links to exactly one existing memory"
            " file that has no metadata.idx yet, and the generated header and preamble must stay unchanged."
            " Put the line into that memory file's frontmatter (metadata.idx) instead; the index is rebuilt"
            " automatically. `cc-memory-index build` lists the reasons."
        )
    if code == EXIT_CONCURRENT:
        return "A memory file changed while the index was rebuilt; it will be rebuilt after the next edit."
    if code == EXIT_PARSE:
        return f"A memory file's frontmatter could not be parsed, so it is indexed by its file name only. {SEE_LINT}"
    return NOT_REBUILT.get(code, f"The index was not rebuilt (exit {code}).") + " " + SEE_LINT


def describe(result: BuildResult) -> Optional[str]:
    """What Claude should know about this rebuild, or None when there is nothing to say."""
    stats: Stats = result.stats or {}
    manual = _count(stats.get("manual_added"))
    parts = [
        _rescued(stats),
        f"{manual} line(s) without a memory-file link were kept as manual index lines." if manual else None,
        _outcome(result.code, stats),
    ]
    text = " ".join(part for part in parts if part)
    return PREFIX + text[:MAX_CONTEXT_CHARS] if text else None


def discard(stream: IO[str]) -> None:
    """Point stream's file descriptor at the null device once its reader has gone away, so the
    interpreter's final flush of what is still buffered cannot fail (it would exit 120)."""
    with contextlib.suppress(OSError, ValueError):
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null, stream.fileno())
        finally:
            os.close(null)


def _send(stream: Optional[IO[str]], text: str) -> None:
    """Write and flush text; a closed or unwritable stream is discarded, never raised."""
    if stream is None:
        return
    try:
        stream.write(text)
        stream.flush()
    except (OSError, ValueError):  # BrokenPipeError; EINVAL for a closed pipe on Windows; encoding
        discard(stream)


def _context(stdin: IO[bytes], env: Mapping[str, str]) -> Optional[str]:
    if env.get(ENV_DISABLE) == "1":
        return None
    store = target_store(stdin.read(MAX_INPUT_BYTES + 1))
    return None if store is None else describe(run_build(store, force=False))


def main(
    stdin: Optional[IO[bytes]] = None,
    stdout: Optional[IO[str]] = None,
    stderr: Optional[IO[str]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    try:
        context = _context(stdin or sys.stdin.buffer, os.environ if env is None else env)
    except Exception as e:  # noqa: BLE001 - the hook must never fail a tool call
        _send(stderr or sys.stderr, f"cc-memory-index hook: {type(e).__name__}: {str(e)[:200]}\n")
        return EXIT_OK
    if context:
        output = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}
        _send(stdout or sys.stdout, json.dumps(output) + "\n")
    return EXIT_OK
