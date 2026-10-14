"""Bookkeeping for the generated files: render record, edit detection and L0 backups (DESIGN §6, §8)."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .safeio import Fingerprint, StoreDir
from .store import L0_NAME, Source, _reject_duplicates

MIGRATING_MARK = ".index-migrating"
RECORD_VERSION = 1
RECORD_MAX_BYTES = 64 * 1024
GENERATED_MAX_BYTES = 4 * 1024 * 1024
BACKUP_PREFIX = f".{L0_NAME}.bak-"
BACKUPS_KEPT = 5
_BACKUP_RE = re.compile(r"\A" + re.escape(BACKUP_PREFIX) + r"([0-9]{8}T[0-9]{6}Z)(?:-([0-9]{1,6}))?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class Current:
    """A generated or bookkeeping file as read under the lock; data and fp are None when absent."""

    name: str
    data: Optional[bytes]
    fp: Optional[Fingerprint]


def read_current(sd: StoreDir, name: str, max_bytes: int = GENERATED_MAX_BYTES) -> Current:
    if sd.kind(name) == "missing":
        return Current(name, None, None)
    data, fp = sd.read(name, max_bytes)
    return Current(name, data, fp)


def as_text(data: Optional[bytes]) -> Optional[str]:
    """UTF-8 text with CRLF normalised to LF, or None when absent or not UTF-8."""
    if data is None:
        return None
    try:
        return data.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        return None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def has_content(data: Optional[bytes]) -> bool:
    """True unless the file is absent, empty, or holds only blank lines and level-1 titles."""
    if data is None:
        return False
    text = as_text(data)
    if text is None:
        return True
    for line in text.splitlines():
        bare = line.strip().lstrip("﻿")
        if bare and bare != "#" and not bare.startswith("# "):
            return True
    return False


def record_bytes(l0: str, l1: str) -> bytes:
    """.index-render.json for the given L0 and L1 text."""
    obj = {"version": RECORD_VERSION, "l0": sha256_text(l0), "l1": sha256_text(l1)}
    return (json.dumps(obj, indent=2) + "\n").encode("utf-8")


def parse_record(current: Current) -> Optional[dict[str, str]]:
    """{'l0': sha, 'l1': sha} from .index-render.json, or None when absent or malformed."""
    text = as_text(current.data)
    if text is None:
        return None
    try:
        obj = json.loads(text, object_pairs_hook=_reject_duplicates)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or set(obj) != {"version", "l0", "l1"}:
        return None
    if type(obj["version"]) is not int or obj["version"] != RECORD_VERSION:
        return None
    hashes = {role: obj[role] for role in ("l0", "l1")}
    if not all(isinstance(h, str) and _SHA256.match(h) for h in hashes.values()):
        return None
    return hashes


def needs_migration(l0: Current, record: Current, sources: Sequence[Source]) -> bool:
    """DESIGN §8: a hand-written MEMORY.md, no render record and no source with a tier."""
    if record.data is not None or not has_content(l0.data):
        return False
    return not any(src.fm.get("tier") is not None for src in sources)


def edited_generated_files(
    record: Optional[Mapping[str, str]], current: Mapping[str, Current], fresh: Mapping[str, str]
) -> list[str]:
    """Names of generated files whose current content writing the fresh render would lose.

    current and fresh are keyed by role ('l0', 'l1'). A file is not edited when it is absent,
    has no content beyond titles, equals the fresh render (an interrupted build converges), or
    matches the hash this tool recorded when it last wrote it. CRLF is normalised first.
    """
    edited: list[str] = []
    for role, cur in current.items():
        if not has_content(cur.data):
            continue
        text = as_text(cur.data)
        if text is not None and (text == fresh[role] or (record is not None and sha256_text(text) == record[role])):
            continue
        edited.append(cur.name)
    return edited


def _backup_key(name: str) -> Optional[tuple[str, int]]:
    m = _BACKUP_RE.match(name)
    return (m.group(1), int(m.group(2) or 0)) if m else None


def backup_l0(sd: StoreDir, data: bytes, now: Optional[float] = None) -> str:
    """Copy L0 to .MEMORY.md.bak-<UTC timestamp> and keep the newest BACKUPS_KEPT backups.

    Within one second the suffix continues after the highest one present, and the backup just
    written is never pruned, even when the clock has stepped back behind older backups.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))
    same_second = [key[1] for key in map(_backup_key, sd.names()) if key is not None and key[0] == stamp]
    name = BACKUP_PREFIX + stamp + (f"-{max(same_second) + 1}" if same_second else "")
    sd.replace(name, data, None)
    _prune_backups(sd, name)
    return name


def _prune_backups(sd: StoreDir, keep: str) -> None:
    keyed: list[tuple[tuple[str, int], str]] = []
    for name in sd.names():
        key = _backup_key(name)
        if key is not None and name != keep:
            keyed.append((key, name))
    for _, name in sorted(keyed)[: max(0, len(keyed) - (BACKUPS_KEPT - 1))]:
        if sd.kind(name) == "file" and sd.nlink(name) == 1:
            sd.remove(name, sd.fingerprint(name))
