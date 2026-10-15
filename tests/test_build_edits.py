from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from storekit import append, built_store, sample_store, snapshot, write_memory

from cc_memory_index.build import run_build, run_check
from cc_memory_index.generated import Current, Record, backup_generated, read_record, record_bytes
from cc_memory_index.safeio import StoreDir

L0, L1, MARK = "MEMORY.md", "index-l1.md", ".index-render.json"


@pytest.mark.parametrize("target", [L0, L1])
def test_unrescuable_edit_is_refused_and_nothing_is_written(tmp_path: Path, target: str) -> None:
    root = built_store(tmp_path)
    write_memory(root, "new.md", name="New", tier="resident")
    append(root, target, "- [both](build.md) and [tea](tea.md)\n")
    before = snapshot(root)
    res = run_build(str(root), False)
    assert res.code == 3 and target in res.messages[-1] and "--force" in res.messages[-1]
    assert "frontmatter" in res.messages[0] and res.stats["unrescuable"][0]["segment"].startswith("[both]")
    assert snapshot(root) == before


def _backups(root: Path, name: str = L0) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.name.startswith(f".{name}.bak-"))


def test_force_backs_up_l0_and_keeps_the_newest_five(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    old = [f".MEMORY.md.bak-2020010{n}T000000Z" for n in range(1, 7)]
    for name in old:
        (root / name).write_text("old", encoding="utf-8")
    (root / L0).write_text("# Memory Index\n- precious hand-written line\n", encoding="utf-8")
    res = run_build(str(root), True)
    assert res.code == 0 and res.stats["written"] == [L0]
    (new,) = res.stats["backups"]
    assert _backups(root) == old[2:] + [new] and _backups(root, L1) == []
    assert (root / new).read_text(encoding="utf-8") == "# Memory Index\n- precious hand-written line\n"
    assert "precious" not in (root / L0).read_text(encoding="utf-8") and run_check(str(root)).code == 0


def test_force_backs_up_both_generated_files(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    append(root, L0, "- [x](gone.md) — unrescuable\n")
    append(root, L1, "- kept only in the backup\n")
    l0, l1 = (root / L0).read_bytes(), (root / L1).read_bytes()
    assert run_build(str(root), False).code == 3
    res = run_build(str(root), True)
    assert res.code == 0 and res.stats["written"] == [L1, L0]
    b0, b1 = res.stats["backups"]
    assert b0.startswith(".MEMORY.md.bak-") and b1.startswith(".index-l1.md.bak-")
    assert (root / b0).read_bytes() == l0 and (root / b1).read_bytes() == l1
    assert "--force" in res.messages[-1] and b1 in res.messages[-1]
    assert run_check(str(root)).code == 0 and not (root / ".index-manual.json").exists()


def test_backup_names_never_collide(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    with StoreDir(str(root)) as sd:
        names = [backup_generated(sd, L0, f"v{n}".encode(), now=0.0) for n in range(12)]
        other = backup_generated(sd, L1, b"l1", now=0.0)
    assert names[:3] == [".MEMORY.md.bak-19700101T000000Z", ".MEMORY.md.bak-19700101T000000Z-1",
                         ".MEMORY.md.bak-19700101T000000Z-2"]
    assert names[11] == ".MEMORY.md.bak-19700101T000000Z-11"
    assert _backups(root) == sorted(names[7:]) and other == ".index-l1.md.bak-19700101T000000Z"
    assert [(root / n).read_text(encoding="utf-8") for n in names[7:]] == [f"v{n}" for n in range(7, 12)]


def test_backup_survives_a_clock_that_stepped_back(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    with StoreDir(str(root)) as sd:
        newer = [backup_generated(sd, L1, f"new {n}".encode(), now=2e9 + n) for n in range(5)]
        older = backup_generated(sd, L1, b"after the clock stepped back", now=1e9)
    assert _backups(root, L1) == sorted([older] + newer[1:])


def test_a_tier_set_by_hand_does_not_make_a_store_migrated(tmp_path: Path) -> None:
    """Only the render record or the manual-lines file says a store was migrated (DESIGN §8)."""
    root = sample_store(tmp_path)
    for text in ("- something by hand\n", "Some text by hand\n"):
        (root / L0).write_text(text, encoding="utf-8")
        before = snapshot(root)
        res = run_build(str(root), False)
        assert (res.code, res.messages) == (2, [f"{L0} was written by hand: run `cc-memory-index migrate` first"])
        assert snapshot(root) == before
    (root / ".index-manual.json").write_text('{"version": 1, "preamble": null, "lines": []}', encoding="utf-8")
    res = run_build(str(root), False)
    assert res.code == 3 and "preamble edited" in res.messages[0]
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
    (root / MARK).write_text("{oops", encoding="utf-8")
    res = run_build(str(root), False)
    assert res.code == 0 and "malformed" in res.messages[0] and res.stats["written"] == [MARK]
    (root / MARK).write_text("{oops", encoding="utf-8")
    (root / L0).write_text("hand-written text\n", encoding="utf-8")
    assert run_build(str(root), False).code == 3


def test_record_without_l1_name_means_the_default(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    (root / MARK).write_text((root / MARK).read_text(encoding="utf-8").replace(',\n  "l1_name": "index-l1.md"', ""), encoding="utf-8")
    append(root, L1, "- an L1 line added by hand\n")
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["manual_added"] == ["an L1 line added by hand"]
    assert not any("malformed" in m for m in res.messages)


SHA_A, SHA_B = "a" * 64, "b" * 64


def _record(obj: object) -> Current:
    return Current(MARK, json.dumps(obj).encode(), None)


def test_record_round_trip() -> None:
    data = record_bytes("l0\n", "l1\n", "more.md", {"old.md": SHA_A})
    record = read_record(Current(MARK, data, None))
    assert record is not None and (record.l1_name, dict(record.stale)) == ("more.md", {"old.md": SHA_A})
    assert "stale" not in json.loads(record_bytes("a", "b"))
    assert read_record(_record({"version": 1, "l0": SHA_A, "l1": SHA_B})) == Record(SHA_A, SHA_B, L1, {})
    assert Record(SHA_A, SHA_B, L1, {"old.md": SHA_A}).accepts("l1", "old.md", SHA_A)
    assert not Record(SHA_A, SHA_B).accepts("l0", L0, SHA_B)


@pytest.mark.parametrize("obj", [
    {"version": 1, "l0": SHA_A},
    {"version": 1, "l0": SHA_A, "l1": SHA_B, "extra": 1},
    {"version": 1, "l0": SHA_A, "l1": SHA_B, "l1_name": "MEMORY.md"},
    {"version": 1, "l0": SHA_A, "l1": SHA_B, "l1_name": "../x.md"},
    {"version": 1, "l0": SHA_A, "l1": SHA_B, "stale": []},
    {"version": 1, "l0": SHA_A, "l1": SHA_B, "stale": {"x.txt": SHA_A}},
    {"version": 1, "l0": SHA_A, "l1": SHA_B, "stale": {"x.md": "short"}},
])
def test_malformed_records_are_rejected(obj: object) -> None:
    assert read_record(_record(obj)) is None


def _spy(monkeypatch: pytest.MonkeyPatch, method: str) -> list[str]:
    seen: list[str] = []
    real = getattr(StoreDir, method)

    def spy(self: StoreDir, name: str, *args: Any) -> Any:
        seen.append(name)
        return real(self, name, *args)

    monkeypatch.setattr(StoreDir, method, spy)
    return seen


def test_backups_are_compared_by_size_first_and_pruned_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = built_store(tmp_path)
    names = [f".MEMORY.md.bak-2020010{n}T000000Z" for n in range(1, 8)]
    for n, name in enumerate(names):
        (root / name).write_bytes(b"x" * n)
    hashed = _spy(monkeypatch, "fingerprint")
    with StoreDir(str(root)) as sd:
        first = backup_generated(sd, L0, b"new content")
        assert hashed == [] and _backups(root) == names[-4:] + [first]
        (root / first).write_bytes(b"old content")
        assert backup_generated(sd, L0, b"new content") != first and hashed == [first]
