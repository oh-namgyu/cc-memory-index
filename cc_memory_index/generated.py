"""Bookkeeping for the generated files: render record, edit detection, stale L1 files and backups
(DESIGN §6, §8)."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from .safeio import Fingerprint, SafeIOError, StoreDir
from .store import L0_NAME, Config, Source, reject_duplicates, valid_source_name

MIGRATING_MARK = ".index-migrating"
RECORD_VERSION = 1
RECORD_MAX_BYTES = 64 * 1024
GENERATED_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_L1_NAME = Config.l1_name
BACKUPS_KEPT = 5
_STAMP = re.compile(r"([0-9]{8}T[0-9]{6}Z)(?:-([0-9]{1,6}))?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RECORD_KEYS = frozenset({"version", "l0", "l1", "l1_name", "stale"})


@dataclass(frozen=True)
class Current:
    """A generated or bookkeeping file as read under the lock; data and fp are None when absent."""

    name: str
    data: Optional[bytes]
    fp: Optional[Fingerprint]


@dataclass(frozen=True)
class Record:
    """.index-render.json: hashes of L0 and L1 as last written, the L1 name they were written
    under, and earlier L1 files (from before l1_name changed) still exactly as written."""

    l0: str
    l1: str
    l1_name: str = DEFAULT_L1_NAME
    stale: Mapping[str, str] = field(default_factory=dict)

    def accepts(self, role: str, name: str, sha: str) -> bool:
        """True when sha is content this tool wrote for that role."""
        if role == "l0":
            return sha == self.l0
        return sha == self.l1 or self.stale.get(name) == sha


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


def record_bytes(l0: str, l1: str, l1_name: str = DEFAULT_L1_NAME, stale: Optional[Mapping[str, str]] = None) -> bytes:
    """.index-render.json for the given L0 and L1 text; 'stale' is written only when there are any."""
    obj: dict[str, object] = {"version": RECORD_VERSION, "l0": sha256_text(l0), "l1": sha256_text(l1),
                              "l1_name": l1_name}
    if stale:
        obj["stale"] = dict(sorted(stale.items()))
    return (json.dumps(obj, indent=2) + "\n").encode("utf-8")


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA256.match(value) is not None


def _is_l1_name(value: object) -> bool:
    return isinstance(value, str) and valid_source_name(value) and value.lower() != L0_NAME.lower()


def read_record(current: Current) -> Optional[Record]:
    """The render record, or None when absent or malformed. A record without l1_name means the default."""
    text = as_text(current.data)
    if text is None:
        return None
    try:
        obj = json.loads(text, object_pairs_hook=reject_duplicates)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or not {"version", "l0", "l1"} <= set(obj) <= _RECORD_KEYS:
        return None
    if type(obj["version"]) is not int or obj["version"] != RECORD_VERSION:
        return None
    l1_name, stale = obj.get("l1_name", DEFAULT_L1_NAME), obj.get("stale", {})
    if not (_is_sha(obj["l0"]) and _is_sha(obj["l1"]) and _is_l1_name(l1_name) and isinstance(stale, dict)):
        return None
    if not all(_is_l1_name(name) and _is_sha(sha) for name, sha in stale.items()):
        return None
    return Record(obj["l0"], obj["l1"], l1_name, dict(stale))


def parse_record(current: Current) -> Optional[dict[str, str]]:
    """{'l0': sha, 'l1': sha, 'l1_name': name} from .index-render.json, or None when absent or malformed."""
    record = read_record(current)
    return None if record is None else {"l0": record.l0, "l1": record.l1, "l1_name": record.l1_name}


def needs_migration(l0: Current, record: Current, sources: Sequence[Source], has_manual: bool) -> bool:
    """DESIGN §8: a hand-written MEMORY.md, no render record, no manual-lines file (migrating an
    index that links no memory file writes only that) and no source with a tier."""
    if record.data is not None or has_manual or not has_content(l0.data):
        return False
    return not any(src.fm.get("tier") is not None for src in sources)


def edited_generated_files(
    record: Optional[Record], current: Mapping[str, Current], fresh: Mapping[str, str]
) -> list[str]:
    """Roles ('l0', 'l1') of the generated files whose current content writing the fresh render would lose.

    current and fresh are keyed by role. A file is not edited when it is absent, has no content
    beyond titles, equals the fresh render (an interrupted build converges), or matches what this
    tool recorded when it last wrote it. CRLF is normalised first.
    """
    edited: list[str] = []
    for role, cur in current.items():
        if not has_content(cur.data):
            continue
        text = as_text(cur.data)
        if text is not None and (
            text == fresh[role] or (record is not None and record.accepts(role, cur.name, sha256_text(text)))
        ):
            continue
        edited.append(role)
    return edited


def stale_generated(sd: StoreDir, record: Optional[Record], l1_name: str) -> dict[str, str]:
    """Earlier L1 files, written under another l1_name and unchanged since: name -> SHA-256.

    They are not memory files and stay out of the scan until deleted; one that was edited since
    is an ordinary source again.
    """
    if record is None:
        return {}
    candidates = {**record.stale, record.l1_name: record.l1}
    found: dict[str, str] = {}
    for name, sha in sorted(candidates.items()):
        if name.lower() in (l1_name.lower(), L0_NAME.lower()) or sd.kind(name) != "file":
            continue
        try:
            text = as_text(read_current(sd, name).data)
        except (SafeIOError, OSError):
            continue
        if text is not None and sha256_text(text) == sha:
            found[name] = sha
    return found


def backup_prefix(name: str) -> str:
    return f".{name}.bak-"


def _backup_key(prefix: str, name: str) -> Optional[tuple[str, int]]:
    m = _STAMP.match(name[len(prefix):]) if name.startswith(prefix) else None
    return (m.group(1), int(m.group(2) or 0)) if m else None


def backup_generated(sd: StoreDir, name: str, data: bytes, now: Optional[float] = None) -> str:
    """Copy a generated file to .<name>.bak-<UTC timestamp> and keep its newest BACKUPS_KEPT backups.

    Within one second the suffix continues after the highest one present, and the backup just
    written is never pruned, even when the clock has stepped back behind older backups. When the
    newest backup already holds exactly data (a rerun after a failed write), it is kept instead of
    a copy, so a rerun ends with the backups an uninterrupted run leaves (DESIGN §7.2).
    """
    prefix = backup_prefix(name)
    keyed = _backups(sd, prefix)
    if keyed and _holds(sd, keyed[-1][1], data):
        backup = keyed[-1][1]
    else:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))
        same_second = [key[1] for key, _ in keyed if key[0] == stamp]
        backup = prefix + stamp + (f"-{max(same_second) + 1}" if same_second else "")
        sd.replace(backup, data, None)
    _prune_backups(sd, prefix, backup)
    return backup


def _backups(sd: StoreDir, prefix: str) -> list[tuple[tuple[str, int], str]]:
    """(key, name) of every backup with this prefix, oldest first."""
    keyed: list[tuple[tuple[str, int], str]] = []
    for name in sd.names():
        key = _backup_key(prefix, name)
        if key is not None:
            keyed.append((key, name))
    return sorted(keyed)


def _holds(sd: StoreDir, name: str, data: bytes) -> bool:
    """True when name is a regular, singly linked file whose content is exactly data."""
    if sd.kind(name) != "file" or sd.nlink(name) != 1:
        return False
    return sd.fingerprint(name) == Fingerprint(len(data), hashlib.sha256(data).hexdigest())


def _prune_backups(sd: StoreDir, prefix: str, keep: str) -> None:
    keyed = [(key, name) for key, name in _backups(sd, prefix) if name != keep]
    for _, name in keyed[: max(0, len(keyed) - (BACKUPS_KEPT - 1))]:
        if sd.kind(name) == "file" and sd.nlink(name) == 1:
            sd.remove(name, sd.fingerprint(name))
