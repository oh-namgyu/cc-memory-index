from __future__ import annotations

import json
from pathlib import Path

import pytest
from storekit import snapshot, tree
from test_migrate_apply import hand_store, record_replaces

from cc_memory_index.migrate import run_migrate, summary
from cc_memory_index.migrate_snapshot import SNAPSHOT_PREFIX

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
    with (root / "a.md").open("a") as f:
        f.write("edited after the migration\n")
    with (root / L0).open("a") as f:
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
    path.write_text(change(json.loads(path.read_text())))  # type: ignore[operator]
    before = snapshot(root)
    res = run_migrate(str(root), restore=ident)
    assert res.code == 2 and "manifest" in res.messages[-1] and snapshot(root) == before


def test_restore_refuses_a_tampered_copy(tmp_path: Path) -> None:
    root, original, ident = _migrated(tmp_path)
    patched = (root / "a.md").read_bytes()
    (root / (SNAPSHOT_PREFIX + ident) / "a.md").write_text("tampered")
    res = run_migrate(str(root), restore=ident)
    assert res.code == 4 and res.stats["conflicts"] == ["a.md"] and "does not match" in res.messages[0]
    assert (root / "a.md").read_bytes() == patched and (root / "b.md").read_bytes() == original["b.md"]


def test_restore_keeps_a_marker_of_another_migration(tmp_path: Path) -> None:
    root, original, ident = _migrated(tmp_path)
    (root / MIGRATING).write_text("20000101T000000Z\n")
    res = run_migrate(str(root), restore=ident)
    assert res.code == 0 and "another migration" in res.messages[0]
    assert (root / MIGRATING).read_text() == "20000101T000000Z\n"
    assert {n: d for n, d in _top(root).items() if n != MIGRATING} == original
