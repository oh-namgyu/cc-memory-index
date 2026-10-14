from __future__ import annotations

from pathlib import Path

import pytest
from storekit import built_store, memory_text, snapshot, write_memory

from cc_memory_index import frontmatter
from cc_memory_index.build import run_check
from cc_memory_index.demote import demote_files, run_demote
from cc_memory_index.frontmatter import parse

L0, L1 = "MEMORY.md", "index-l1.md"


def _changed_lines(before: bytes, after: bytes) -> list[tuple[bytes, bytes]]:
    old, new = before.split(b"\n"), after.split(b"\n")
    assert len(old) == len(new)
    return [(a, b) for a, b in zip(old, new) if a != b]


def test_only_the_tier_line_changes(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    before = (root / "build.md").read_bytes()
    res = run_demote(str(root), "build.md")
    assert res.code == 0 and res.stats["demoted"] == ["build.md"]
    assert res.messages[0] == "build.md: tier set to indexed"
    after = (root / "build.md").read_bytes()
    assert _changed_lines(before, after) == [(b'  tier: "resident"', b'  tier: "indexed"')]
    assert "build.md" not in (root / L0).read_text() and "[Build](build.md)" in (root / L1).read_text()
    assert run_check(str(root)).code == 0


def test_crlf_and_bare_values_are_kept(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    before = b"---\r\nname: tea\r\nmetadata:\r\n  tier: resident   # pinned\r\n  type: user\r\n---\r\nbody\r\n"
    (root / "tea.md").write_bytes(before)
    assert run_demote(str(root), "tea.md").code == 0
    assert _changed_lines(before, (root / "tea.md").read_bytes()) == [
        (b"  tier: resident   # pinned\r", b'  tier: "indexed"\r')
    ]


def test_top_level_tier_is_replaced_in_place(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    before = b"---\nname: tea\ntier: resident\ndescription: likes tea\n---\nbody\n"
    (root / "tea.md").write_bytes(before)
    assert run_demote(str(root), "tea.md").code == 0
    assert _changed_lines(before, (root / "tea.md").read_bytes()) == [(b"tier: resident", b'tier: "indexed"')]


def test_absent_tier_is_added_and_nothing_else_changes(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    before = (root / "old.md").read_bytes()
    res = run_demote(str(root), "old.md")
    assert res.code == 0 and res.stats["demoted"] == ["old.md"]
    after = (root / "old.md").read_bytes()
    assert parse(after.decode()).get("tier") == "indexed"
    added = [line for line in after.split(b"\n") if line not in before.split(b"\n")]
    assert added == [b'  tier: "indexed"']
    assert after.replace(b'  tier: "indexed"\n', b"", 1) == before


def test_already_indexed_writes_nothing(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    run_demote(str(root), "old.md")
    before = snapshot(root)
    res = run_demote(str(root), "old.md")
    assert res.code == 0 and res.stats["demoted"] == [] and res.messages[0] == "old.md already has tier indexed"
    assert snapshot(root) == before


@pytest.mark.parametrize("name", ["missing.md", "../build.md", "sub/build.md", "_build.md", "a..b.md", "build",
                                  L0, L1, "Build.md"])
def test_unknown_or_invalid_names_exit_2(tmp_path: Path, name: str) -> None:
    root = built_store(tmp_path)
    before = snapshot(root)
    res = run_demote(str(root), name)
    assert res.code == 2 and len(res.messages) == 1
    assert snapshot(root) == before


def test_non_source_entries_exit_2(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    (root / "dir.md").mkdir()
    (root / "binary.md").write_bytes(b"---\nname: \xff\n---\n")
    for name in ("dir.md", "binary.md"):
        assert run_demote(str(root), name).code == 2


def test_unparseable_frontmatter_exits_7(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    (root / "open.md").write_text("---\nname: open\nno closing line\n")
    before = snapshot(root)
    res = run_demote(str(root), "open.md")
    assert res.code == 7 and "set its tier by hand" in res.messages[0]
    assert snapshot(root) == before


def test_concurrent_edit_exits_4_and_keeps_the_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = built_store(tmp_path)
    real = frontmatter.patch
    edited = memory_text(name="Build", description="edited meanwhile", tier="resident")

    def racing(text: str, add: dict[str, str], replace: dict[str, str]) -> str:
        (root / "build.md").write_text(edited)
        return real(text, add, replace)

    monkeypatch.setattr(frontmatter, "patch", racing)
    l0 = (root / L0).read_bytes()
    res = run_demote(str(root), "build.md")
    assert res.code == 4 and "edited concurrently" in res.messages[0]
    assert (root / "build.md").read_text() == edited and (root / L0).read_bytes() == l0


def test_migration_marker_refuses(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    (root / ".index-migrating").write_text("")
    res = run_demote(str(root), "build.md")
    assert res.code == 2 and "migrate --apply" in res.messages[0]


def test_demote_files_stops_at_the_first_failure(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    write_memory(root, "x.md", name="x", tier="resident")
    res = demote_files(str(root), ["x.md", "missing.md", "tea.md"])
    assert res.code == 2 and res.stats["demoted"] == ["x.md"]
    assert parse((root / "tea.md").read_text()).get("tier") == "resident"
