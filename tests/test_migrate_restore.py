from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from storekit import make_store, snapshot, tree, write_memory
from test_migrate_apply import hand_store, record_replaces

from cc_memory_index.build import run_build
from cc_memory_index.migrate import run_migrate, summary
from cc_memory_index import migrate_snapshot as snapshot_mod
from cc_memory_index.migrate_snapshot import SNAPSHOT_PREFIX
from cc_memory_index.safeio import StoreDir

L0, L1, MIGRATING, RECORD = "MEMORY.md", "index-l1.md", ".index-migrating", ".index-render.json"


def _top(root: Path) -> dict[str, bytes]:
    return {name: data for name, data in tree(root).items() if not name.startswith(SNAPSHOT_PREFIX)}


def _migrated(tmp_path: Path, built: bool = False) -> tuple[Path, dict[str, bytes], str]:
    """A migrated store; built=True also requires the first build to have written the index."""
    root = hand_store(tmp_path)
    original = _top(root)
    res = run_migrate(str(root), apply=True)
    assert "snapshot" in res.stats and not (root / MIGRATING).exists(), res.messages
    assert not built or (res.code == 0 and (root / RECORD).is_file()), res.messages
    return root, original, res.stats["snapshot"][len(SNAPSHOT_PREFIX):]


def test_restore_round_trip(tmp_path: Path) -> None:
    root, original, ident = _migrated(tmp_path, built=True)
    res = run_migrate(str(root), restore=ident)
    assert res.code == 0 and res.stats["conflicts"] == [], res.messages
    assert _top(root) == original and (root / (SNAPSHOT_PREFIX + ident) / "manifest.json").is_file()
    assert summary(res) == [f"restored 7 file(s) from {SNAPSHOT_PREFIX}{ident}; 0 left alone"]
    assert "index-notes.md" in res.stats["restored"]
    assert run_migrate(str(root), restore=ident).code == 0 and _top(root) == original
    again = run_migrate(str(root), apply=True)
    assert again.code == 0 and again.stats["snapshot"] != SNAPSHOT_PREFIX + ident


def test_restore_leaves_files_edited_since(tmp_path: Path) -> None:
    root, original, ident = _migrated(tmp_path, built=True)
    with (root / "a.md").open("a", encoding="utf-8") as f:
        f.write("edited after the migration\n")
    with (root / L0).open("a", encoding="utf-8") as f:
        f.write("- a line Claude added\n")
    edited = {name: (root / name).read_bytes() for name in ("a.md", L0)}
    res = run_migrate(str(root), restore=ident)
    assert res.code == 4 and res.stats["conflicts"] == [L0, "a.md"], res.messages
    assert {name: (root / name).read_bytes() for name in edited} == edited
    assert (root / "b.md").read_bytes() == original["b.md"] and not (root / L1).exists()
    assert (root / RECORD).is_file() and not (root / ".index-manual.json").exists()
    assert all(m.startswith("left alone: ") for m in res.messages)


def test_restore_after_a_crash_at_every_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as m:
        calls = record_replaces(m)
        run_migrate(str(hand_store(tmp_path / "reference")), apply=True)
    for k in range(2, len(calls) + 1):
        root = hand_store(tmp_path / f"crash{k}")
        original = _top(root)
        with monkeypatch.context() as m:
            record_replaces(m, crash_at=k)
            assert run_migrate(str(root), apply=True).code == 6
        ident = next(p.name for p in root.iterdir() if p.name.startswith(SNAPSHOT_PREFIX))[len(SNAPSHOT_PREFIX):]
        res = run_migrate(str(root), restore=ident)
        if res.code == 0:
            assert _top(root) == original, (k, calls[k - 1])
        else:
            assert res.code == 4 and set(res.stats["conflicts"]) <= {L0, L1}, (k, res.messages)
            assert {n: d for n, d in _top(root).items() if n not in (L0, L1)} == {
                n: d for n, d in original.items() if n != L0}, k
        assert not (root / MIGRATING).exists()


@pytest.mark.parametrize("ident", ["latest", "../20260101T000000Z", "20260101T000000Z/..", ""])
def test_restore_rejects_bad_ids(tmp_path: Path, ident: str) -> None:
    root, _, _ = _migrated(tmp_path)
    before = snapshot(root)
    res = run_migrate(str(root), restore=ident)
    assert res.code == 2 and "snapshot id" in res.messages[-1] and snapshot(root) == before


def test_restore_of_a_missing_snapshot(tmp_path: Path) -> None:
    root, _, _ = _migrated(tmp_path)
    res = run_migrate(str(root), restore="20000101T000000Z")
    assert res.code == 2 and "no snapshot" in res.messages[-1]


@pytest.mark.parametrize("change", [
    lambda m: "{",
    lambda m: json.dumps({**m, "version": 2}),
    lambda m: json.dumps({**m, "files": {"../a.md": m["files"]["a.md"]}}),
    lambda m: json.dumps({**m, "files": {"a.md": {"before": "x", "after": None}}}),
    lambda m: json.dumps({**m, "extra": 1}),
])
def test_restore_refuses_a_malformed_manifest(tmp_path: Path, change: object) -> None:
    root, _, ident = _migrated(tmp_path)
    path = root / (SNAPSHOT_PREFIX + ident) / "manifest.json"
    path.write_text(change(json.loads(path.read_text(encoding="utf-8"))), encoding="utf-8")  # type: ignore[operator]
    before = snapshot(root)
    res = run_migrate(str(root), restore=ident)
    assert res.code == 2 and "manifest" in res.messages[-1] and snapshot(root) == before


def test_restore_refuses_a_tampered_copy(tmp_path: Path) -> None:
    root, original, ident = _migrated(tmp_path)
    patched = (root / "a.md").read_bytes()
    (root / (SNAPSHOT_PREFIX + ident) / "a.md").write_text("tampered", encoding="utf-8")
    res = run_migrate(str(root), restore=ident)
    assert res.code == 4 and res.stats["conflicts"] == ["a.md"] and "does not match" in res.messages[0]
    assert (root / "a.md").read_bytes() == patched and (root / "b.md").read_bytes() == original["b.md"]


def test_restore_keeps_a_marker_of_another_migration(tmp_path: Path) -> None:
    root, original, ident = _migrated(tmp_path)
    (root / MIGRATING).write_text("20000101T000000Z\n", encoding="utf-8")
    res = run_migrate(str(root), restore=ident)
    assert res.code == 0 and "another migration" in res.messages[0]
    assert (root / MIGRATING).read_text(encoding="utf-8") == "20000101T000000Z\n"
    assert {n: d for n, d in _top(root).items() if n != MIGRATING} == original


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _craft(root: Path, **changes: object) -> None:
    record = json.loads((root / RECORD).read_text(encoding="utf-8"))
    (root / RECORD).write_text(json.dumps({**record, **changes}), encoding="utf-8")


def test_a_crafted_stale_entry_neither_hides_nor_deletes_a_memory(tmp_path: Path) -> None:
    """Audit PoC 2c: a 'stale' entry hid victim.md from the index, build said to delete it, and
    migrate --restore then deleted it."""
    root = make_store(tmp_path)
    write_memory(root, "a.md", name="A", description="a")
    write_memory(root, "victim.md", name="victim", description="an important memory")
    assert run_build(str(root), False).code == 0
    victim = (root / "victim.md").read_bytes()
    _craft(root, stale={"victim.md": _sha(root / "victim.md")})
    res = run_build(str(root), False)
    assert res.code == 0 and not any("stale" in m for m in res.messages)
    assert "victim.md" in (root / L1).read_text(encoding="utf-8")
    assert "stale" not in json.loads((root / RECORD).read_text(encoding="utf-8"))
    _craft(root, stale={"victim.md": _sha(root / "victim.md")})
    snap = root / (SNAPSHOT_PREFIX + "20260101T000000Z")
    snap.mkdir()
    (snap / "manifest.json").write_text(json.dumps({"version": 1, "created": "x", "files": {}, "manual_after": None}),
                                        encoding="utf-8")
    run_migrate(str(root), restore="20260101T000000Z")
    assert (root / "victim.md").read_bytes() == victim


@pytest.mark.parametrize("field", ["stale", "l1_name"])
def test_restore_takes_no_names_from_the_render_record(tmp_path: Path, field: str) -> None:
    root, original, ident = _migrated(tmp_path, built=True)
    write_memory(root, "victim.md", name="victim", description="written after the migration")
    with (root / "b.md").open("a", encoding="utf-8") as f:
        f.write("edited after the migration\n")
    kept = {name: (root / name).read_bytes() for name in ("victim.md", "b.md")}
    if field == "stale":
        _craft(root, stale={"victim.md": _sha(root / "victim.md"), "b.md": _sha(root / "b.md")})
    else:
        _craft(root, l1_name="b.md", l1=_sha(root / "b.md"))
    res = run_migrate(str(root), restore=ident)
    assert res.code == 4 and "b.md" in res.stats["conflicts"], res.messages
    assert {name: (root / name).read_bytes() for name in kept} == kept
    assert (root / "a.md").read_bytes() == original["a.md"]


def test_a_configured_l1_name_that_is_a_memory_is_never_removed(tmp_path: Path) -> None:
    root, _, ident = _migrated(tmp_path, built=True)
    write_memory(root, "victim.md", name="victim", description="written after the migration")
    victim = (root / "victim.md").read_bytes()
    (root / ".memory-index.json").write_text(json.dumps({"l1_name": "victim.md"}), encoding="utf-8")
    _craft(root, l1_name="victim.md", l1=_sha(root / "victim.md"))
    run_migrate(str(root), restore=ident)
    assert (root / "victim.md").read_bytes() == victim


def test_restore_reads_each_file_once_and_never_an_oversized_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, original, ident = _migrated(tmp_path, built=True)
    (root / "a.md").write_bytes(b"x" * 9000)
    monkeypatch.setattr(snapshot_mod, "MANIFEST_MAX_BYTES", 8192)
    reads: list[tuple[str, str]] = []
    for method in ("read", "fingerprint"):
        real = getattr(StoreDir, method)

        def spy(self: StoreDir, name: str, *args: Any, _real: Any = real, _method: str = method) -> Any:
            if self.path == os.path.realpath(root):
                reads.append((_method, name))
            return _real(self, name, *args)

        monkeypatch.setattr(StoreDir, method, spy)
    res = run_migrate(str(root), restore=ident)
    assert res.code == 4 and res.stats["conflicts"] == ["a.md"] and "a.md changed after" in res.messages[0]
    assert not any(name == "a.md" for _, name in reads)
    for name in ("b.md", L0):
        assert reads.count(("read", name)) == 1 and reads.count(("fingerprint", name)) == 1, (name, reads)
    assert {name: (root / name).read_bytes() for name in ("b.md", L0)} == {name: original[name] for name in ("b.md", L0)}
