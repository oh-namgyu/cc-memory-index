from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable

import pytest
from storekit import built_store, make_store, sample_store, snapshot, write_json, write_memory

from cc_memory_index import plan as plan_mod
from cc_memory_index.build import run_build, run_check
from cc_memory_index.items import MANUAL_NAME, PENDING_NAME
from cc_memory_index.render import link_line
from cc_memory_index.safeio import StoreDir

L0, L1, MARK = "MEMORY.md", "index-l1.md", ".index-render.json"


def test_build_writes_l1_l0_and_record(tmp_path: Path) -> None:
    root = sample_store(tmp_path)
    res = run_build(str(root), False)
    assert (res.code, res.messages) == (0, [])
    assert res.stats["written"] == [L1, L0, MARK]
    l0, l1 = (root / L0).read_bytes(), (root / L1).read_bytes()
    assert b"\r" not in l0 + l1 and b"[Build](build.md)" in l0 and b"[Old](old.md)" in l1
    record = json.loads((root / MARK).read_text())
    assert record == {"version": 1, "l0": hashlib.sha256(l0).hexdigest(), "l1": hashlib.sha256(l1).hexdigest(),
                      "l1_name": L1}
    assert res.stats["l0_bytes"] == len(l0) and res.stats["l0_lines"] == l0.count(b"\n")


def test_rebuild_is_byte_identical_and_writes_nothing(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    before = snapshot(root)
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["written"] == []
    assert snapshot(root) == before
    twin = tmp_path / "twin"
    shutil.copytree(root, twin)
    for name in (L0, L1, MARK, ".index.lock"):
        (twin / name).unlink()
    assert run_build(str(twin), False).code == 0
    assert all((twin / n).read_bytes() == (root / n).read_bytes() for n in (L0, L1, MARK))


def test_check_is_read_only_and_tracks_sync(tmp_path: Path) -> None:
    root = sample_store(tmp_path)
    res = run_check(str(root))
    assert res.code == 1 and res.stats["out_of_sync"] == [L0, L1]
    assert not (root / ".index.lock").exists() and not (root / L0).exists()
    run_build(str(root), False)
    assert run_check(str(root)).code == 0
    before = snapshot(root)
    with (root / L0).open("a") as f:
        f.write("- [hand](tea.md) — added by hand\n")
    res = run_check(str(root))
    assert res.code == 1 and res.stats["out_of_sync"] == [L0]
    assert snapshot(root)[L1] == before[L1]


def test_unmigrated_store_is_refused(tmp_path: Path) -> None:
    root = sample_store(tmp_path)
    for name in ("build.md", "tea.md"):
        write_memory(root, name, name=name)
    (root / L0).write_text("- [Build](build.md) — written by hand\n")
    before = snapshot(root)
    res = run_build(str(root), False)
    assert res.code == 2 and "migrate" in res.messages[-1]
    assert run_check(str(root)).code == 1
    assert snapshot(root) == before


@pytest.mark.parametrize("text", ["", "\n\n", "# Memory Index\n", "﻿# Title\n\n"])
def test_empty_or_title_only_memory_md_is_fine(tmp_path: Path, text: str) -> None:
    root = sample_store(tmp_path)
    (root / L0).write_text(text, encoding="utf-8")
    assert run_build(str(root), False).code == 0
    assert link_line(L1) in (root / L0).read_text()


def test_migration_marker_blocks_every_command(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    (root / ".index-migrating").write_text("")
    write_memory(root, "new.md", name="New")
    before = snapshot(root)
    res = run_build(str(root), False)
    assert res.code == 2 and "migrate --apply" in res.messages[-1]
    assert run_check(str(root)).code == 1
    assert snapshot(root) == before


def _race(monkeypatch: pytest.MonkeyPatch, action: Callable[[], object]) -> None:
    real = plan_mod.render

    def racing(*args: Any, **kwargs: Any) -> Any:
        out = real(*args, **kwargs)
        action()
        return out

    monkeypatch.setattr(plan_mod, "render", racing)


def test_concurrent_edit_exits_4_and_leaves_the_file_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = built_store(tmp_path)
    write_memory(root, "new.md", name="New", tier="resident")
    write_memory(root, "later.md", name="Later")
    record = (root / MARK).read_bytes()
    _race(monkeypatch, lambda: (root / L0).write_text("edited meanwhile\n"))
    res = run_build(str(root), False)
    assert res.code == 4 and "concurrently" in res.messages[-1]
    assert (root / L0).read_text() == "edited meanwhile\n" and (root / MARK).read_bytes() == record
    assert res.stats["written"] == [L1]


def test_lock_timeout_exits_5(tmp_path: Path) -> None:
    root = built_store(tmp_path, {"lock_timeout_s": 1})
    write_memory(root, "new.md", name="New")
    before = snapshot(root)
    with StoreDir(str(root)) as other, other.lock(1):
        res = run_build(str(root), False)
    assert res.code == 5 and snapshot(root) == before


def test_unparseable_sources_are_reported_with_exit_7(tmp_path: Path) -> None:
    root = sample_store(tmp_path)
    (root / "bin.md").write_bytes(b"---\nname: \xff\n---\n")
    (root / "broken.md").write_text("---\nname: never closed\n")
    (root / "bad name.md").write_text("ignored here, reported by lint")
    res = run_build(str(root), False)
    assert res.code == 7 and res.stats["written"] == [L1, L0, MARK]
    joined = "\n".join(res.messages)
    assert "bin.md" in joined and "broken.md" in joined and "bad name" not in joined
    assert "- [broken](broken.md)\n" in (root / L1).read_text()
    assert run_check(str(root)).code == 7


def test_overflow_exits_1_and_still_writes(tmp_path: Path) -> None:
    root = make_store(tmp_path, {"budget_bytes": 2048})
    for n in range(20):
        write_memory(root, f"m{n:02d}.md", tier="resident", idx=f"[m{n:02d}](m{n:02d}.md) — " + "x" * 150)
    write_memory(root, "zz.md", name="unparseable sibling")
    (root / "bin.md").write_bytes(b"\xff")
    res = run_build(str(root), False)
    assert res.code == 1 and res.stats["overflow"] > 0 and "suggest" in res.messages[-1]
    assert len((root / L0).read_bytes()) <= 2048 and "## Overflow" in (root / L1).read_text()
    assert run_check(str(root)).code == 7


def test_unreachable_item_exits_8_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = sample_store(tmp_path)
    real = plan_mod.render

    def lossy(*args: Any, **kwargs: Any) -> Any:
        out = real(*args, **kwargs)
        return dataclasses.replace(out, l1=out.l1.replace("- [Old](old.md) — an old project note\n", ""))

    monkeypatch.setattr(plan_mod, "render", lossy)
    res = run_build(str(root), False)
    assert res.code == 8 and "old.md" in res.messages[-1]
    assert not (root / L0).exists() and not (root / L1).exists() and not (root / MARK).exists()


@pytest.mark.parametrize("name, content", [
    (".memory-index.json", '{"budget_bytes": 2048, "item_max_bytes": 2000}'),
    (".memory-index.json", '{"bogus": 1}'),
    pytest.param(".memory-index.json", "[" * 60000, id="deeply-nested"),
    (MANUAL_NAME, '{"version": 1}'),
    (PENDING_NAME, '{"version": 1, "entries": [{"file": "../x.md", "idx": "x", "section": "", "sub": "", "order": 0}]}'),
])
def test_configuration_errors_exit_2(tmp_path: Path, name: str, content: str) -> None:
    root = sample_store(tmp_path)
    (root / name).write_text(content)
    res = run_build(str(root), False)
    assert res.code == 2 and res.messages
    assert not (root / L0).exists()


def test_pending_entries_are_rendered_and_folded(tmp_path: Path) -> None:
    root = sample_store(tmp_path)
    write_memory(root, "same.md", idx="already folded")
    write_memory(root, "conflict.md", idx="the file's own line")
    entries = [
        {"file": "tea.md", "idx": "[Tea](tea.md) — rescued line", "section": "Rescued", "sub": "", "order": 1},
        {"file": "same.md", "idx": "already  folded", "section": "", "sub": "", "order": 0},
        {"file": "conflict.md", "idx": "[c](conflict.md) — rescued but different", "section": "", "sub": "", "order": 0},
    ]
    write_json(root, PENDING_NAME, {"version": 1, "entries": entries})
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["unresolved"] == 1
    l0, l1 = (root / L0).read_text(), (root / L1).read_text()
    assert "## Rescued\n- [Tea](tea.md) — rescued line\n" in l0
    assert l1.count("already folded") == 1 and "the file's own line" in l1
    assert l1.endswith("## Unresolved\n- [c](conflict.md) — rescued but different\n")
    assert json.loads((root / PENDING_NAME).read_text())["entries"] == entries[2:]
    assert '  idx: "[Tea](tea.md) — rescued line"\n  idx_section: "Rescued"\n' in (root / "tea.md").read_text()
    assert run_check(str(root)).code == 0


def test_manual_preamble_and_lines_are_rendered(tmp_path: Path) -> None:
    root = sample_store(tmp_path)
    write_json(root, MANUAL_NAME, {"version": 1, "preamble": "# My memory\n\nRead me first.", "lines": [
        {"seg": "Ask before deleting data", "section": "Rules", "sub": "", "order": 0}]})
    assert run_build(str(root), False).code == 0
    l0 = (root / L0).read_text()
    assert l0.startswith("# My memory\n\nRead me first.\n\n" + link_line(L1))
    assert "## Rules\n- Ask before deleting data\n" in l0


def test_linked_l0_is_refused(tmp_path: Path, symlink: Callable[[Path, Path], None]) -> None:
    root = sample_store(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("keep me")
    symlink(root / L0, outside)
    res = run_build(str(root), False)
    assert res.code == 2 and outside.read_text() == "keep me"
