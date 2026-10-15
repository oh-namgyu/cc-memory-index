"""DESIGN §7.1: temporary files left by a killed write are removed by the next writing command."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import pytest
from storekit import built_store, make_store, write_index, write_memory

from cc_memory_index.build import run_build, run_check
from cc_memory_index.demote import run_demote
from cc_memory_index.migrate import run_migrate
from cc_memory_index.migrate_snapshot import SNAPSHOT_PREFIX
from cc_memory_index.result import BuildResult
from cc_memory_index.safeio import StoreDir
from cc_memory_index.suggest import run_suggest

HEX = "0123456789abcdef"
OLD = 11 * 60


def _temp(root: Path, name: str = f".MEMORY.md.tmp-{HEX}", age_s: int = OLD, data: str = "half a write") -> Path:
    path = root / name
    path.write_text(data, encoding="utf-8")
    then = time.time() - age_s
    os.utime(path, (then, then))
    return path


def test_old_temporary_files_are_removed_and_fresh_ones_kept(tmp_path: Path) -> None:
    root = make_store(tmp_path)
    old, fresh = _temp(root), _temp(root, f".a.md.tmp-{HEX[::-1]}", age_s=60)
    keep = [_temp(root, name) for name in (f".a.md.tmp-{HEX[:15]}", f".a.md.tmp-{HEX.upper()}", f"a.md.tmp-{HEX}",
                                           f".a.md.tmp-{HEX}0", f"..tmp-{HEX}")]
    with StoreDir(str(root)) as sd:
        assert sd.sweep_temp() == 1
    assert not old.exists() and fresh.exists() and all(path.exists() for path in keep)


def test_links_directories_and_hard_links_are_never_touched(
    tmp_path: Path, symlink: Callable[[Path, Path], None]
) -> None:
    root = make_store(tmp_path)
    target = _temp(tmp_path, "outside.md")
    link = root / f".MEMORY.md.tmp-{HEX}"
    symlink(link, target)
    if os.utime in os.supports_follow_symlinks:
        os.utime(link, (time.time() - OLD, time.time() - OLD), follow_symlinks=False)
    folder = root / f".b.md.tmp-{HEX}"
    folder.mkdir()
    os.utime(folder, (time.time() - OLD, time.time() - OLD))
    twin = _temp(root, f".c.md.tmp-{HEX}")
    os.link(twin, tmp_path / "twin")
    with StoreDir(str(root)) as sd:
        assert sd.sweep_temp() == 0
    assert link.is_symlink() and target.read_text(encoding="utf-8") == "half a write" and folder.is_dir() and twin.is_file()


@pytest.mark.parametrize("command", [
    lambda root: run_build(str(root), False),
    lambda root: run_demote(str(root), "build.md"),
    lambda root: run_migrate(str(root), apply=True),
    lambda root: run_suggest(str(root), apply=True),
])
def test_writing_commands_sweep_and_report_it(tmp_path: Path, command: Callable[[Path], BuildResult]) -> None:
    root = built_store(tmp_path)
    old = _temp(root)
    res = command(root)
    assert res.code == 0 and res.stats["temp_removed"] == 1 and not old.exists()
    assert run_check(str(root)).code == 0


def test_read_only_commands_leave_temporary_files(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    old = _temp(root)
    assert run_check(str(root)).code == 0 and run_migrate(str(root)).code == 0 and old.exists()


def test_migrate_sweeps_its_snapshot_and_restore_the_store(tmp_path: Path) -> None:
    root = make_store(tmp_path)
    write_memory(root, "a.md", name="a")
    write_index(root, "## S\n- [A](a.md) — x\n")
    (root / f"{SNAPSHOT_PREFIX}20260101T000000Z").mkdir()
    (root / ".index-migrating").write_text("20260101T000000Z\n", encoding="utf-8")
    _temp(root / f"{SNAPSHOT_PREFIX}20260101T000000Z", f".manifest.json.tmp-{HEX}")
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and res.stats["temp_removed"] == 1, res.messages
    assert (root / f"{SNAPSHOT_PREFIX}20260101T000000Z" / "manifest.json").is_file() and not list(root.rglob("*.tmp-*"))
    _temp(root)
    res = run_migrate(str(root), restore="20260101T000000Z")
    assert res.code == 0 and res.stats["temp_removed"] == 1
    assert (root / "MEMORY.md").read_text(encoding="utf-8") == "## S\n- [A](a.md) — x\n"
