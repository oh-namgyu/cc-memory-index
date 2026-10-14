from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from storekit import built_store, sample_store, snapshot, write_memory

from cc_memory_index.build import run_build, run_check
from cc_memory_index.generated import backup_l0
from cc_memory_index.safeio import StoreDir

L0, L1, MARK = "MEMORY.md", "index-l1.md", ".index-render.json"


@pytest.mark.parametrize("target", [L0, L1])
def test_hand_edit_is_refused_and_nothing_is_written(tmp_path: Path, target: str) -> None:
    root = built_store(tmp_path)
    write_memory(root, "new.md", name="New", tier="resident")
    with (root / target).open("a") as f:
        f.write("- a line added by hand\n")
    before = snapshot(root)
    res = run_build(str(root), False)
    assert res.code == 3 and target in res.messages[-1] and "--force" in res.messages[-1]
    assert snapshot(root) == before


def _backups(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.name.startswith(".MEMORY.md.bak-"))


def test_force_backs_up_l0_and_keeps_the_newest_five(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    old = [f".MEMORY.md.bak-2020010{n}T000000Z" for n in range(1, 7)]
    for name in old:
        (root / name).write_text("old")
    (root / L0).write_text("# Memory Index\n- precious hand-written line\n")
    res = run_build(str(root), True)
    assert res.code == 0 and res.stats["written"] == [L0]
    new = res.stats["backup"]
    assert _backups(root) == old[2:] + [new]
    assert (root / new).read_text() == "# Memory Index\n- precious hand-written line\n"
    assert "precious" not in (root / L0).read_text() and run_check(str(root)).code == 0


def test_backup_names_never_collide(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    with StoreDir(str(root)) as sd:
        names = [backup_l0(sd, f"v{n}".encode(), now=0.0) for n in range(12)]
    assert names[:3] == [".MEMORY.md.bak-19700101T000000Z", ".MEMORY.md.bak-19700101T000000Z-1",
                         ".MEMORY.md.bak-19700101T000000Z-2"]
    assert names[11] == ".MEMORY.md.bak-19700101T000000Z-11"
    assert _backups(root) == sorted(names[7:])
    assert [(root / n).read_text() for n in names[7:]] == [f"v{n}" for n in range(7, 12)]


def test_backup_survives_a_clock_that_stepped_back(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    with StoreDir(str(root)) as sd:
        newer = [backup_l0(sd, b"new", now=2e9 + n) for n in range(5)]
        older = backup_l0(sd, b"after the clock stepped back", now=1e9)
    assert _backups(root) == sorted([older] + newer[1:])


def test_tiered_store_without_record_treats_l0_as_edited(tmp_path: Path) -> None:
    root = sample_store(tmp_path)
    (root / L0).write_text("- something by hand\n")
    assert run_build(str(root), False).code == 3
    assert run_build(str(root), True).code == 0


def test_crlf_conversion_is_not_an_edit(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    (root / L0).write_bytes((root / L0).read_bytes().replace(b"\n", b"\r\n"))
    assert run_check(str(root)).code == 0
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["written"] == [L0]
    assert b"\r" not in (root / L0).read_bytes()


def test_interrupted_build_converges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = built_store(tmp_path)
    write_memory(root, "new.md", name="New", tier="resident")
    write_memory(root, "later.md", name="Later")
    real = StoreDir.replace

    def crash_on_l0(self: StoreDir, name: str, data: bytes, expect: Any) -> Any:
        if name == L0:
            raise OSError("disk full")
        return real(self, name, data, expect)

    monkeypatch.setattr(StoreDir, "replace", crash_on_l0)
    res = run_build(str(root), False)
    assert res.code == 6 and res.stats["written"] == [L1]
    monkeypatch.setattr(StoreDir, "replace", real)
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["written"] == [L0, MARK]


def test_malformed_record(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    (root / MARK).write_text("{oops")
    res = run_build(str(root), False)
    assert res.code == 0 and "malformed" in res.messages[0] and res.stats["written"] == [MARK]
    (root / MARK).write_text("{oops")
    (root / L0).write_text("- hand edit\n")
    assert run_build(str(root), False).code == 3
