"""PostToolUse hook entry point (DESIGN §11).

Claude Code runs this after every Write, Edit or MultiEdit. It rebuilds the index only for a
memory file that sits directly inside a store this tool has already written, and tells Claude
what happened through `additionalContext`. It always exits 0: a bug here must never block a
session.
"""

from __future__ import annotations

import json
import os
import sys
from typing import IO, Any, Mapping, Optional

from .build import BuildResult, run_build
from .store import store_root_for_edit

MAX_INPUT_BYTES = 1 << 20
MAX_CONTEXT_CHARS = 2000
TOOLS = frozenset({"Write", "Edit", "MultiEdit"})
ENV_DISABLE = "CC_MEMORY_INDEX_DISABLE"


def target_store(raw: bytes) -> Optional[str]:
    """The store to rebuild for this hook payload, or None when the call is not ours."""
    if len(raw) > MAX_INPUT_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("tool_name") not in TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    return store_root_for_edit(file_path) if isinstance(file_path, str) else None


def _names(entries: Any, key: str) -> list[str]:
    if not isinstance(entries, list):
        return []
    return [str(e.get(key, "")) for e in entries if isinstance(e, dict)]


def describe(result: BuildResult) -> Optional[str]:
    """What Claude should know about this rebuild, or None when there is nothing to say."""
    stats: Mapping[str, Any] = result.stats or {}
    lines = []
    rescued = _names(stats.get("rescued"), "file")
    if rescued:
        lines.append(
            "MEMORY.md is generated from each memory file's frontmatter. The line you added for "
            + ", ".join(rescued)
            + " was moved into that file's metadata.idx and is listed in the on-demand index."
            " Set metadata.tier: resident in a memory file to load it every session."
        )
    manual = stats.get("manual_added")
    if isinstance(manual, list) and manual:
        lines.append(f"{len(manual)} line(s) without a memory-file link were kept as manual index lines.")
    if result.code == 1:
        lines.append(
            f"{stats.get('overflow', 'Some')} resident entries no longer fit the MEMORY.md budget and were"
            " listed in the on-demand index instead. Run `cc-memory-index suggest` to choose memories to demote."
        )
    elif result.code == 3:
        reasons = _names(stats.get("unrescuable"), "reason") or result.messages[:3]
        lines.append(
            "Your edit to the generated index was not applied ("
            + "; ".join(reasons[:3])
            + "). Edit the memory file's frontmatter (metadata.idx) instead; the index is rebuilt automatically."
        )
    elif result.code == 4:
        lines.append("A memory file changed while the index was rebuilt; it will be rebuilt after the next edit.")
    elif result.code not in (0, 5, 7):
        lines.append("cc-memory-index could not rebuild the index: " + "; ".join(result.messages[:3]))
    text = " ".join(lines)
    return "cc-memory-index: " + text[:MAX_CONTEXT_CHARS] if text else None


def main(
    stdin: Optional[IO[bytes]] = None,
    stdout: Optional[IO[str]] = None,
    stderr: Optional[IO[str]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    env = os.environ if env is None else env
    try:
        if env.get(ENV_DISABLE) == "1":
            return 0
        store = target_store(stdin.read(MAX_INPUT_BYTES + 1))
        if store is None:
            return 0
        context = describe(run_build(store, force=False))
        if context:
            output = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}
            stdout.write(json.dumps(output) + "\n")
    except Exception as e:  # noqa: BLE001 - the hook must never fail a tool call
        stderr.write(f"cc-memory-index hook: {type(e).__name__}: {str(e)[:200]}\n")
    return 0
