from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest
from storekit import make_store, memory_text, snapshot, tree, write_index, write_memory

from cc_memory_index import migrate as migrate_mod
from cc_memory_index.build import run_build
from cc_memory_index.frontmatter import parse
from cc_memory_index.migrate import run_migrate
from cc_memory_index.migrate_plan import render_store
from cc_memory_index.migrate_snapshot import SNAPSHOT_PREFIX, new_snapshot_id
from cc_memory_index.safeio import StoreDir

L0, MIGRATING, RECORD = "MEMORY.md", ".index-migrating", ".index-render.json"
INDEX = ("# Memory\n\n## Rules\n- [A](a.md) — first · [B](b.md) — second\n  - a detail under A\n- free text\n"
         "### Sub\n- [A](a.md) — again\n```\n- code, not an index line\n```\n")
NOTES = "index-notes.md"


def hand_store(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    root = make_store(base)
    for name in ("a.md", "b.md", "c.md"):
        write_memory(root, name, name=name[:-3])
    write_index(root, INDEX)
    return root


def record_replaces(monkeypatch: pytest.MonkeyPatch, crash_at: int = 0) -> list[str]:
    """Record every StoreDir.replace (snapshot copies included); raise OSError on call crash_at."""
    calls: list[str] = []
    real = StoreDir.replace

    def replace(self: StoreDir, name: str, data: bytes, expect: Any) -> Any:
        calls.append(name)
        if len(calls) == crash_at:
            raise OSError("simulated crash")
        return real(self, name, data, expect)

    monkeypatch.setattr(StoreDir, "replace", replace)
    return calls


def _tier(root: Path, name: str) -> Any:
    return parse((root / name).read_text(encoding="utf-8")).metadata.get("tier")


def test_crash_at_every_replace_then_rerun_converges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reference = hand_store(tmp_path / "reference")
    with monkeypatch.context() as m:
        calls = record_replaces(m)
        uninterrupted = run_migrate(str(reference), apply=True).code
    expected = tree(reference)
    assert calls[0] == MIGRATING and calls.count("manifest.json") == 1
    for k in range(1, len(calls) + 1):
        root = hand_store(tmp_path / f"crash{k}")
        with monkeypatch.context() as m:
            record_replaces(m, crash_at=k)
            assert run_migrate(str(root), apply=True).code == 6, k
        res = run_migrate(str(root), apply=True)
        assert res.code == uninterrupted, (k, calls[k - 1], res.messages)
        assert tree(root) == expected, (k, calls[k - 1])


def test_resume_plans_from_the_snapshot_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = hand_store(tmp_path)
    with monkeypatch.context() as m:
        calls = record_replaces(m, crash_at=9)
        assert run_migrate(str(root), apply=True).code == 6 and calls[-2:] == [NOTES, "a.md"]
    write_index(root, INDEX + "- [C](c.md) — written after the crash\n")
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and res.messages[0].startswith("resuming the interrupted migration"), res.messages
    assert _tier(root, "a.md") == _tier(root, "b.md") == "resident" and _tier(root, "c.md") is None
    assert "written after the crash" in (root / "c.md").read_text(encoding="utf-8") + (root / "index-l1.md").read_text(encoding="utf-8")


def test_concurrent_edit_stops_and_a_rerun_snapshots_the_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = hand_store(tmp_path)
    edited = memory_text(name="b", description="edited meanwhile")
    real = StoreDir.replace

    def racing(self: StoreDir, name: str, data: bytes, expect: Any) -> Any:
        if name == "b.md" and self.path == os.path.realpath(root):
            (root / "b.md").write_text(edited, encoding="utf-8")
            monkeypatch.setattr(StoreDir, "replace", real)
        return real(self, name, data, expect)

    monkeypatch.setattr(StoreDir, "replace", racing)
    res = run_migrate(str(root), apply=True)
    assert res.code == 4 and "migrate --apply" in res.messages[-1], res.messages
    assert (root / MIGRATING).is_file() and (root / "b.md").read_text(encoding="utf-8") == edited
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and _tier(root, "b.md") == "resident" and "edited meanwhile" in (root / "b.md").read_text(encoding="utf-8")
    ident = res.stats["snapshot"][len(SNAPSHOT_PREFIX):]
    assert run_migrate(str(root), restore=ident).code == 0
    assert (root / "b.md").read_text(encoding="utf-8") == edited


def test_failed_check_keeps_the_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = hand_store(tmp_path)
    def lossy(*args: Any) -> Any:
        out = render_store(*args)
        return dataclasses.replace(out, l0=out.l0.replace("free text", "something else"))

    with monkeypatch.context() as m:
        m.setattr(migrate_mod, "render_store", lossy)
        res = run_migrate(str(root), apply=True)
    assert res.code == 1 and "'free text'" in res.messages[-1] and "--restore" in res.messages[-1]
    assert (root / MIGRATING).is_file() and not (root / RECORD).exists() and _tier(root, "a.md") == "resident"
    assert run_build(str(root), False).code == 2 and run_migrate(str(root)).code == 1
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and not (root / MIGRATING).exists() and (root / RECORD).is_file()


def test_a_marker_that_names_no_snapshot_is_refused(tmp_path: Path) -> None:
    root = hand_store(tmp_path)
    (root / MIGRATING).write_text("not a snapshot id\n", encoding="utf-8")
    before = snapshot(root)
    assert run_migrate(str(root)).code == 1
    res = run_migrate(str(root), apply=True)
    assert res.code == 2 and "does not name a snapshot" in res.messages[-1]
    assert snapshot(root) == before


def test_lock_timeout_exits_5_and_writes_nothing(tmp_path: Path) -> None:
    root = hand_store(tmp_path)
    (root / ".memory-index.json").write_text(json.dumps({"lock_timeout_s": 1}), encoding="utf-8")
    before = snapshot(root)
    with StoreDir(str(root)) as other, other.lock(1):
        assert run_migrate(str(root), apply=True).code == 5
        assert run_migrate(str(root), restore="20260101T000000Z").code == 5
    assert snapshot(root) == before


def test_apply_and_restore_together_is_a_usage_error(tmp_path: Path) -> None:
    root = hand_store(tmp_path)
    before = snapshot(root)
    assert run_migrate(str(root), apply=True, restore="20260101T000000Z").code == 2
    assert snapshot(root) == before


def test_snapshot_ids_never_collide(tmp_path: Path) -> None:
    root = make_store(tmp_path)
    with StoreDir(str(root)) as sd:
        first = new_snapshot_id(sd, now=0.0)
        for suffix in ("", "-1"):
            (root / f"{SNAPSHOT_PREFIX}{first}{suffix}").mkdir()
        assert (first, new_snapshot_id(sd, now=0.0)) == ("19700101T000000Z", "19700101T000000Z-2")


def test_manifest_records_every_copy_and_its_intended_hash(tmp_path: Path) -> None:
    root = hand_store(tmp_path)
    (root / ".index-pending.json").write_text('{"version": 1, "entries": []}', encoding="utf-8")
    res = run_migrate(str(root), apply=True)
    assert res.code == 0, res.messages
    snap = root / res.stats["snapshot"]
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert sorted(manifest["files"]) == [".index-pending.json", L0, "a.md", "b.md", "c.md", NOTES]
    assert manifest["files"]["c.md"]["after"] is None and manifest["files"]["a.md"]["after"] is not None
    assert manifest["files"][NOTES]["before"] is None and not (snap / NOTES).exists()
    assert (snap / L0).read_text(encoding="utf-8") == INDEX
    assert os.name != "posix" or snap.stat().st_mode & 0o777 == 0o700
