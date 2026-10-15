"""Migration snapshots (.index-backup-<id>/manifest.json) and migrate --restore (DESIGN §8).

The manifest records, for every copied file, its SHA-256 before the migration and the one the
migration means to write; the notes file the migration creates has no before-hash. The intended
hash is recorded before the file is written, so a run that stops anywhere can still be restored:
a file that holds its after-hash is put back (or removed, when the migration created it), one
that holds its before-hash was never changed, and anything else was edited since and is left
alone. The render record gives only the hashes of MEMORY.md and the configured L1, never names.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

from .generated import MIGRATING_MARK, RECORD_MAX_BYTES, Current, Record, as_text, is_l1_text, read_current, read_record
from .items import MANUAL_NAME, PENDING_NAME
from .jsonfile import JSONFileError, parse_object
from .result import EXIT_CONCURRENT, EXIT_USAGE, BuildResult, Refusal
from .safeio import Fingerprint, StoreDir, is_sha256
from .stamps import parse_stamp, utc_stamp, with_suffix
from .store import CONFIG_NAME, L0_NAME, RENDER_MARK, Config, valid_source_name

SNAPSHOT_PREFIX = ".index-backup-"
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
MANIFEST_MAX_BYTES = 4 * 1024 * 1024
MARKER_MAX_BYTES = 4096
DATA_NAMES = (MANUAL_NAME, PENDING_NAME, CONFIG_NAME)
_MANIFEST_KEYS = {"version", "created", "files", "manual_after"}
Target = Tuple[str, Optional[str], Optional[str]]
Now = Tuple[Optional[Current], Optional[str]]
UNKNOWN = ""  # an after-hash no file can have: the file is put back only if it still holds its before-hash


def _sha(data: bytes) -> str:
    return Fingerprint.of(data).sha256


@dataclass
class Entry:
    before: Optional[str]
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
    return value is None or is_sha256(value)


def _entry(name: object, value: object) -> Optional[Entry]:
    if not isinstance(name, str) or not (valid_source_name(name) or name in DATA_NAMES):
        return None
    if not isinstance(value, dict) or set(value) != {"before", "after"}:
        return None
    before, after = value["before"], value["after"]
    if not (_sha_or_none(before) and _sha_or_none(after)) or before is after is None:
        return None
    return Entry(before, after)


def parse_manifest(data: bytes, where: str) -> Manifest:
    """A manifest checked strictly; anything unexpected is a usage error (exit 2)."""
    try:
        obj = parse_object(data, where)
    except JSONFileError as e:
        raise Refusal(EXIT_USAGE, str(e)) from e
    bad = Refusal(EXIT_USAGE, f"{where} is not a migration manifest")
    if set(obj) != _MANIFEST_KEYS or obj["version"] != MANIFEST_VERSION:
        raise bad
    if (type(obj["version"]) is not int or not isinstance(obj["created"], str) or not isinstance(obj["files"], dict)
            or not _sha_or_none(obj["manual_after"])):
        raise bad
    files: dict[str, Entry] = {}
    for name, value in obj["files"].items():
        entry = _entry(name, value)
        if entry is None:
            raise bad
        files[name] = entry
    return Manifest(obj["created"], files, obj["manual_after"])


def new_snapshot_id(sd: StoreDir, now: Optional[float] = None) -> str:
    """The UTC stamp of now, with the first -1, -2... suffix that no snapshot has when one has none."""
    stamp, n = utc_stamp(now), 0
    while sd.kind(SNAPSHOT_PREFIX + with_suffix(stamp, n)) != "missing":
        n += 1
    return with_suffix(stamp, n)


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
            manifest.files[name] = Entry(_sha(data), None)
        self.manifest = manifest
        return manifest

    def intend(self, manifest: Manifest, name: str, current: bytes, new: bytes) -> None:
        """Record new as name's after-hash; a file changed since it was copied is copied again."""
        entry = manifest.files.get(name)
        if entry is None or _sha(current) not in (entry.before, entry.after):
            self._put(name, current)
            entry = manifest.files[name] = Entry(_sha(current), None)
        entry.after = _sha(new)

    def intend_new(self, manifest: Manifest, name: str, new: bytes) -> None:
        """Record a file the migration creates: no before-hash, new as its after-hash."""
        manifest.files[name] = Entry(None, _sha(new))

    def created(self) -> Optional[str]:
        """The file the recorded migration creates, or None (no manifest yet, or no such file)."""
        files = {} if self.manifest is None else self.manifest.files
        return next((name for name, entry in sorted(files.items()) if entry.before is None), None)

    def intend_manual(self, manifest: Manifest, current: Optional[bytes], new: bytes) -> None:
        entry = manifest.files.get(MANUAL_NAME)
        if current is not None and _sha(current) not in (manifest.manual_after, entry.before if entry else None):
            self._put(MANUAL_NAME, current)
            manifest.files[MANUAL_NAME] = Entry(_sha(current), None)
        manifest.manual_after = _sha(new)

    def save(self, manifest: Manifest) -> None:
        data = manifest.to_bytes()
        if data != self._saved:
            self._fp = self.dir.replace(MANIFEST_NAME, data, self._fp)
            self._saved = data


def marker_ident(data: Optional[bytes]) -> Optional[str]:
    """The snapshot id a .index-migrating marker names, or None when it names none."""
    text = (as_text(data) or "").strip()
    return text if parse_stamp(text) else None


def _current(sd: StoreDir, name: str) -> Now:
    """name as it is now (data None when missing), or why it is left alone. A file larger than any
    copy a snapshot holds cannot hold a hash the migration recorded, so it is not read."""
    kind = sd.kind(name)
    if kind == "missing":
        return Current(name, None, None), None
    if kind != "file":
        return None, f"{name} is not a regular file"
    if sd.entry_stat(name)[1] > MANIFEST_MAX_BYTES:
        return None, f"{name} changed after the migration"
    return read_current(sd, name, MANIFEST_MAX_BYTES), None


def _generated(record: Optional[Record], cfg: Config, l1: Optional[Current]) -> dict[str, Optional[str]]:
    """MEMORY.md and the configured L1 -> the hash build last wrote (UNKNOWN without a record: a build
    that stopped before writing it may still have written them). The names never come from the record,
    and an L1 that does not start with the generated head is left to the manifest alone."""
    l0_sha = l1_sha = UNKNOWN
    if record is not None:
        l0_sha = record.l0
        l1_sha = record.l1 if record.l1_name == cfg.l1_name else record.stale.get(cfg.l1_name, UNKNOWN)
    found: dict[str, Optional[str]] = {L0_NAME: l0_sha}
    text = None if l1 is None else as_text(l1.data)
    if l1 is None or l1.data is None or (text is not None and is_l1_text(text, cfg.l1_name)):
        found[cfg.l1_name] = l1_sha
    return found


def _targets(manifest: Manifest, generated: Mapping[str, Optional[str]]) -> list[Target]:
    """(name, before, after) for every file the migration or its first build may have written."""
    special = {**generated, MANUAL_NAME: manifest.manual_after}
    files = sorted(manifest.files.items())
    out: list[Target] = [(name, e.before, e.after) for name, e in files if name not in special]
    for name, after in special.items():
        entry = manifest.files.get(name)
        out.append((name, entry.before if entry else None, after))
    return out


def _put_back(sd: StoreDir, snap: StoreDir, target: Target, now: Now) -> tuple[bool, Optional[str]]:
    """Restore one file, as read once (now), if it holds its after-hash: (changed, conflict or None).
    The replace or remove checks the file again (DESIGN §7.3)."""
    name, before, after = target
    current, problem = now
    if current is None:
        return False, problem
    sha = None if current.fp is None else current.fp.sha256
    if sha == before:
        return False, None
    if sha != after:
        return False, f"{name} changed after the migration"
    if before is None:
        sd.remove(name, current.fp)
        return True, None
    if snap.kind(name) != "file":
        return False, f"the snapshot has no copy of {name}"
    data, copy = snap.read(name, MANIFEST_MAX_BYTES)
    if copy.sha256 != before:
        return False, f"the snapshot copy of {name} does not match its recorded hash"
    sd.replace(name, data, current.fp)
    return True, None


def _put_all_back(sd: StoreDir, snap: StoreDir, targets: list[Target],
                  read: Mapping[str, Now]) -> tuple[list[str], dict[str, str]]:
    """Restore every target; one the migration never meant to write (after None) is not even read."""
    restored: list[str] = []
    conflicts: dict[str, str] = {}
    for target in targets:
        if target[2] is None:
            continue
        now = read[target[0]] if target[0] in read else _current(sd, target[0])
        changed, problem = _put_back(sd, snap, target, now)
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
    read = {cfg.l1_name: _current(sd, cfg.l1_name)}
    generated = _generated(parsed, cfg, read[cfg.l1_name][0])
    restored, conflicts = _put_all_back(sd, snap, _targets(manifest, generated), read)
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
    if parse_stamp(ident) is None:
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
    res.stats.update({"mode": "restore", "snapshot": name, "restored": restored, "conflicts": sorted(conflicts)})
    if conflicts:
        res.messages.extend(f"left alone: {message}" for message in conflicts.values())
        res.code = EXIT_CONCURRENT
