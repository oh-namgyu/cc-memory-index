"""Count how many recent transcripts mention each memory file (DESIGN §5.3, §12).

Transcripts are private and untrusted. Only the memory file names asked about are kept, and
only for the transcript being read: no transcript text outlives the chunk being scanned (plus
the overlap carried into the next chunk), and none is printed, logged or put into a message or
an exception. Messages name transcript files only (quoted, as names are untrusted too).
"""

from __future__ import annotations

import os
import re
from typing import Optional

from .safeio import SafeIOError, StoreDir, TooLarge
from .text import quote

MIN_TRANSCRIPTS = 1
MAX_TRANSCRIPTS = 50
CHUNK_BYTES = 1 << 20
FILE_MAX_BYTES = 50 << 20
TOTAL_MAX_BYTES = 200 << 20
OVERLAP_BYTES = 512
SUFFIX = ".jsonl"
# A Windows separator appears JSON-escaped (two backslashes) in the JSONL transcripts.
_MENTION = re.compile(rb"memory(?:/|\\{1,2})([A-Za-z0-9][A-Za-z0-9_.-]*\.md)")
_NOT_NAME = re.compile(rb"[^A-Za-z0-9_.-]")


def check_count(n: int) -> int:
    """n when it is a number of transcripts that suggest may read (DESIGN §12), else ValueError."""
    if isinstance(n, bool) or not MIN_TRANSCRIPTS <= n <= MAX_TRANSCRIPTS:
        raise ValueError(f"the number of transcripts must be between {MIN_TRANSCRIPTS} and {MAX_TRANSCRIPTS}")
    return n


def transcripts_dir(store_path: str) -> str:
    """The store's parent as given, not resolved: the store may be a link into a synced folder,
    while Claude Code keeps the project's transcripts next to the unresolved memory directory."""
    return os.path.dirname(os.path.abspath(store_path))


def newest(sd: StoreDir, n: int) -> list[tuple[str, int]]:
    """The n most recently modified regular *.jsonl entries as (name, size); links are never followed."""
    found: list[tuple[int, str, int]] = []
    for name in sd.names():
        if not name.endswith(SUFFIX):
            continue
        try:
            kind, size, mtime_ns = sd.entry_stat(name)
        except (OSError, SafeIOError):
            continue
        if kind == "file":
            found.append((mtime_ns, name, size))
    found.sort(key=lambda entry: (-entry[0], entry[1]))
    return [(name, size) for _, name, size in found[:n]]


class Mentions:
    """The wanted names mentioned in one transcript, fed one chunk at a time.

    A match is taken only when a byte that cannot belong to a name follows it inside the
    buffer, so a name cut by a chunk boundary is never taken in part; the last OVERLAP_BYTES are
    scanned again with the next chunk, so a mention across a boundary is not lost.
    """

    def __init__(self, wanted: set[str]) -> None:
        self.wanted = wanted
        self.found: set[str] = set()
        self._carry = b""

    def _take(self, buf: bytes, settled_only: bool) -> None:
        for m in _MENTION.finditer(buf):
            if settled_only and _NOT_NAME.search(buf, m.end()) is None:
                continue
            name = m.group(1).decode("ascii")
            if name in self.wanted:
                self.found.add(name)

    def feed(self, chunk: bytes) -> None:
        buf = self._carry + chunk
        self._take(buf, settled_only=True)
        self._carry = buf[-OVERLAP_BYTES:]

    def finish(self) -> set[str]:
        """End of the transcript: whatever is left in the overlap is complete."""
        self._take(self._carry, settled_only=False)
        self._carry = b""
        return self.found


def _scan(sd: StoreDir, name: str, wanted: set[str], limit: int, notes: list[str]) -> tuple[Optional[set[str]], int]:
    """Names mentioned in one transcript (None when it is skipped) and the bytes read."""
    mentions = Mentions(wanted)
    used = 0
    try:
        for piece in sd.iter_chunks(name, CHUNK_BYTES, limit):
            used += len(piece)
            mentions.feed(piece)
    except TooLarge:
        notes.append(f"transcript {quote(name)} skipped: it grew past the read limit while being read")
        return None, limit + 1
    except (OSError, SafeIOError):
        notes.append(f"transcript {quote(name)} skipped: it could not be read as a regular file")
        return None, used
    return mentions.finish(), used


def count_mentions(store_path: str, names: set[str], n: int, notes: Optional[list[str]] = None) -> dict[str, int]:
    """For each name, how many of the n most recent transcripts of the store's project mention
    `memory/<name>` (each transcript counts once per name). Skips and limits are noted in notes."""
    check_count(n)
    out: list[str] = [] if notes is None else notes
    counts = dict.fromkeys(names, 0)
    wanted, read, left = set(names), 0, TOTAL_MAX_BYTES
    with StoreDir(transcripts_dir(store_path)) as sd:
        chosen = newest(sd, n)
        for name, size in chosen:
            if size > FILE_MAX_BYTES:
                out.append(f"transcript {quote(name)} skipped: larger than {FILE_MAX_BYTES >> 20} MiB")
                continue
            if size > left:
                out.append(f"transcripts: stopped before {quote(name)}: {TOTAL_MAX_BYTES >> 20} MiB read in total")
                break
            found, used = _scan(sd, name, wanted, min(FILE_MAX_BYTES, left), out)
            left -= used
            if found is not None:
                read += 1
                for mentioned in found:
                    counts[mentioned] += 1
    out.append(f"transcripts: counted mentions in {read} of the {len(chosen)} most recent (up to {n} asked for)")
    return counts
