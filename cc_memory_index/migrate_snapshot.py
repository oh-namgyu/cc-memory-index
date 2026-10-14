"""Migration snapshots (.index-backup-<id>/manifest.json) and migrate --restore (DESIGN §8).

The manifest records, for every copied file, its SHA-256 before the migration and the one the
migration means to write. The intended hash is recorded before the file is written, so a run
that stops anywhere can still be restored: a file that holds its after-hash is put back, one
that holds its before-hash was never changed, and anything else was edited since and is left
alone.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

from .build import EXIT_CONCURRENT, EXIT_USAGE, BuildResult
from .generated import MIGRATING_MARK, RECORD_MAX_BYTES, Record, as_text, read_current, read_record
from .items import MANUAL_NAME, PENDING_NAME
from .plan import Refusal
from .safeio import Fingerprint, StoreDir
from .store import CONFIG_NAME, L0_NAME, RENDER_MARK, Config, reject_duplicates, valid_source_name

SNAPSHOT_PREFIX = ".index-backup-"
SNAPSHOT_ID = re.compile(r"\A[0-9]{8}T[0-9]{6}Z(?:-[0-9]{1,6})?\Z")
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
MANIFEST_MAX_BYTES = 4 * 1024 * 1024
MARKER_MAX_BYTES = 4096
DATA_NAMES = (MANUAL_NAME, PENDING_NAME, CONFIG_NAME)
_MANIFEST_KEYS = {"version", "created", "files", "manual_after"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
Target = Tuple[str, Optional[str], Optional[str]]
UNKNOWN = ""  # an after-hash no file can have: the file is put back only if it still holds its before-hash


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Entry:
    before: str
    after: Optional[str]


@dataclass
class Manifest:
    created: str
    files: dict[str, Entry] = field(default_factory=dict)
    manual_after: Optional[str] = None

    def to_bytes(self) -> bytes:
        files = {name: {"before": e.before, "after": e.after} for name, e in sorted(self.files.items())}
        obj = {"version": MANIFEST_VERSION, "created": self.created, "files": files, "manual_after": self.manual_after}
        return (json.dumps(obj, indent=2) + "\n").encode("utf-8")


def _sha_or_none(value: object) -> bool:
    return value is None or (isinstance(value, str) and _SHA256.match(value) is not None)


def _entry(name: object, value: object) -> Optional[Entry]:
    if not isinstance(name, str) or not (valid_source_name(name) or name in DATA_NAMES):
        return None
    if not isinstance(value, dict) or set(value) != {"before", "after"}:
        return None
    before, after = value["before"], value["after"]
    if not (isinstance(before, str) and _sha_or_none(before) and _sha_or_none(after)):
        return None
    return Entry(before, after)


def parse_manifest(data: bytes, where: str) -> Manifest:
    """A manifest checked strictly; anything unexpected is a usage error (exit 2)."""
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (ValueError, RecursionError) as e:
        raise Refusal(EXIT_USAGE, f"{where} is not valid JSON: {e}") from e
    bad = Refusal(EXIT_USAGE, f"{where} is not a migration manifest")
    if not isinstance(obj, dict) or set(obj) != _MANIFEST_KEYS or obj["version"] != MANIFEST_VERSION:
        raise bad
    if type(obj["version"]) is not int or not isinstance(obj["created"], str) or not isinstance(obj["files"], dict):
        raise bad
    if not _sha_or_none(obj["manual_after"]):
        raise bad
    files: dict[str, Entry] = {}
    for name, value in obj["files"].items():
        entry = _entry(name, value)
        if entry is None:
            raise bad
        files[name] = entry
    return Manifest(obj["created"], files, obj["manual_after"])


def new_snapshot_id(sd: StoreDir, now: Optional[float] = None) -> str:
    """UTC YYYYmmddTHHMMSSZ, with a -1, -2... suffix when that snapshot already exists."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))
    ident, n = stamp, 0
    while sd.kind(SNAPSHOT_PREFIX + ident) != "missing":
        n += 1
        ident = f"{stamp}-{n}"
    return ident


class Snapshot:
    """One open snapshot directory and its manifest (None until the copies are complete)."""

    def __init__(self, snap: StoreDir, ident: str) -> None:
        self.dir = snap
        self.ident = ident
        current = read_current(snap, MANIFEST_NAME, MANIFEST_MAX_BYTES)
        self._saved: Optional[bytes] = current.data
        self._fp: Optional[Fingerprint] = current.fp
        where = f"{SNAPSHOT_PREFIX}{ident}/{MANIFEST_NAME}"
        self.manifest = None if current.data is None else parse_manifest(current.data, where)

    def _put(self, name: str, data: bytes) -> None:
        current = read_current(self.dir, name)
        if current.data != data:
            self.dir.replace(name, data, current.fp)

    def take(self, originals: Mapping[str, bytes]) -> Manifest:
        """Copy every original into the snapshot; copies left by an interrupted run are refreshed."""
        manifest = Manifest(self.ident)
        for name, data in sorted(originals.items()):
            self._put(name, data)
            manifest.files[name] = Entry(sha256(data), None)
        self.manifest = manifest
        return manifest

    def intend(self, manifest: Manifest, name: str, current: bytes, new: bytes) -> None:
        """Record new as name's after-hash; a file changed since it was copied is copied again."""
        entry = manifest.files.get(name)
        if entry is None or sha256(current) not in (entry.before, entry.after):
            self._put(name, current)
            entry = manifest.files[name] = Entry(sha256(current), None)
        entry.after = sha256(new)

    def intend_manual(self, manifest: Manifest, current: Optional[bytes], new: bytes) -> None:
        entry = manifest.files.get(MANUAL_NAME)
        if current is not None and sha256(current) not in (manifest.manual_after, entry.before if entry else None):
            self._put(MANUAL_NAME, current)
            manifest.files[MANUAL_NAME] = Entry(sha256(current), None)
        manifest.manual_after = sha256(new)

    def save(self, manifest: Manifest) -> None:
        data = manifest.to_bytes()
        if data != self._saved:
            self._fp = self.dir.replace(MANIFEST_NAME, data, self._fp)
            self._saved = data


def marker_ident(data: Optional[bytes]) -> Optional[str]:
    """The snapshot id a .index-migrating marker names, or None when it names none."""
    text = (as_text(data) or "").strip()
    return text if SNAPSHOT_ID.match(text) else None


def _generated(record: Optional[Record], cfg: Config) -> dict[str, Optional[str]]:
    """Generated file name -> the hash build last wrote. Without a record it is UNKNOWN, not None:
    a build that stopped before writing the record may still have written L0 or L1."""
    if record is None:
        return {L0_NAME: UNKNOWN, cfg.l1_name: UNKNOWN}
    return {**record.stale, L0_NAME: record.l0, record.l1_name: record.l1}


def _targets(manifest: Manifest, generated: Mapping[str, Optional[str]]) -> list[Target]:
    """(name, before, after) for every file the migration or its first build may have written."""
    special = {**generated, MANUAL_NAME: manifest.manual_after}
    files = sorted(manifest.files.items())
    out: list[Target] = [(name, e.before, e.after) for name, e in files if name not in special]
    for name, after in special.items():
        entry = manifest.files.get(name)
        out.append((name, entry.before if entry else None, after))
    return out


def _put_back(sd: StoreDir, snap: StoreDir, target: Target) -> tuple[bool, Optional[str]]:
    """Restore one file when it still holds its after-hash: (changed, conflict message or None).

    after is None for a file the migration never meant to write, which is always left as it is.
    """
    name, before, after = target
    if after is None:
        return False, None
    if sd.kind(name) not in ("file", "missing"):
        return False, f"{name} is not a regular file"
    current = sd.fingerprint(name)
    now = current.sha256 if current else None
    if now == before:
        return False, None
    if now != after:
        return False, f"{name} changed after the migration"
    if before is None:
        sd.remove(name, current)
        return True, None
    if snap.kind(name) != "file":
        return False, f"the snapshot has no copy of {name}"
    data, _ = snap.read(name, MANIFEST_MAX_BYTES)
    if sha256(data) != before:
        return False, f"the snapshot copy of {name} does not match its recorded hash"
    sd.replace(name, data, current)
    return True, None


def _put_all_back(sd: StoreDir, snap: StoreDir, targets: list[Target]) -> tuple[list[str], dict[str, str]]:
    restored: list[str] = []
    conflicts: dict[str, str] = {}
    for target in targets:
        changed, problem = _put_back(sd, snap, target)
        if problem:
            conflicts[target[0]] = problem
        elif changed:
            restored.append(target[0])
    return restored, conflicts


def _drop_marker(sd: StoreDir, ident: str, res: BuildResult) -> None:
    marker = read_current(sd, MIGRATING_MARK, MARKER_MAX_BYTES)
    if marker.data is None:
        return
    if marker_ident(marker.data) == ident:
        sd.remove(MIGRATING_MARK, marker.fp)
    else:
        res.messages.append(f"{MIGRATING_MARK} names another migration and was left alone")


def _undo(sd: StoreDir, snap: StoreDir, manifest: Manifest, cfg: Config) -> tuple[list[str], dict[str, str]]:
    """Put files back, then drop the render record unless a generated file was left alone."""
    record = read_current(sd, RENDER_MARK, RECORD_MAX_BYTES)
    parsed = read_record(record)
    generated = _generated(parsed, cfg)
    restored, conflicts = _put_all_back(sd, snap, _targets(manifest, generated))
    if record.data is not None and parsed is None:
        conflicts[RENDER_MARK] = f"{RENDER_MARK} is malformed, so the generated files were not restored"
    elif record.data is not None and not set(conflicts) & set(generated):
        sd.remove(RENDER_MARK, record.fp)
        restored.append(RENDER_MARK)
    return restored, conflicts


def restore(sd: StoreDir, cfg: Config, ident: str, res: BuildResult) -> None:
    """migrate --restore: undo what the migration and its first build wrote, where unchanged since.

    A snapshot without a manifest belongs to a run that stopped while copying, before it changed
    anything but the marker; only the marker is removed then.
    """
    if not SNAPSHOT_ID.match(ident):
        raise Refusal(EXIT_USAGE, f"--restore expects a snapshot id such as 20260101T120000Z, not {ident!r}")
    name = SNAPSHOT_PREFIX + ident
    if sd.kind(name) != "dir":
        raise Refusal(EXIT_USAGE, f"no snapshot {name} in this store")
    restored: list[str] = []
    conflicts: dict[str, str] = {}
    with sd.subdir(name, create=False) as snap:
        manifest = Snapshot(snap, ident).manifest
        if manifest is not None:
            restored, conflicts = _undo(sd, snap, manifest, cfg)
    _drop_marker(sd, ident, res)
    res.stats.update(mode="restore", snapshot=name, restored=restored, conflicts=sorted(conflicts))
    if conflicts:
        res.messages.extend(f"left alone: {message}" for message in conflicts.values())
        res.code = EXIT_CONCURRENT
