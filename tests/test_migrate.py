from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

import pytest
from storekit import built_store, make_store, memory_text, snapshot, tree, write_index, write_memory

from cc_memory_index.frontmatter import parse
from cc_memory_index.migrate import run_migrate, summary
from cc_memory_index.migrate_plan import split_index
from cc_memory_index.render import link_line
from cc_memory_index.text import normalize_ws, split_segments

L0, L1, MANUAL = "MEMORY.md", "index-l1.md", ".index-manual.json"
INTRO = "---\nname: memory-index\ndescription: my own index\n---\n# Project Memory\n> Read this first.\n"


def _store(tmp_path: Path, index: str, *names: str, eol: str = "\n") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = make_store(tmp_path)
    for name in names:
        write_memory(root, name, name=name[:-3], description=f"about {name[:-3]}")
    write_index(root, index, eol)
    return root


def _meta(root: Path, name: str) -> dict[str, str]:
    return parse((root / name).read_text(encoding="utf-8")).metadata


def _pieces(text: str) -> set[str]:
    found: set[str] = set()
    for line in text.split("\n"):
        parts = split_segments(line[2:]) if line.startswith("- ") else [line]
        found.update(normalize_ws(p) for p in parts)
    return found


def _segments(index: str) -> list[str]:
    return [seg.text for seg in split_index(index).segments]


def test_frontmatter_block_and_intro_become_the_preamble(tmp_path: Path) -> None:
    root = _store(tmp_path, INTRO + "\n## Rules\n- [Tea](tea.md) — likes tea\n", "tea.md")
    res = run_migrate(str(root), apply=True)
    assert res.code == 0, res.messages
    assert json.loads((root / MANUAL).read_text(encoding="utf-8"))["preamble"] == INTRO.rstrip("\n")
    assert (root / L0).read_text(encoding="utf-8").startswith(INTRO + "\n" + link_line(L1) + "\n")
    assert _meta(root, "tea.md")["idx"] == "[Tea](tea.md) — likes tea"


SECTIONED = INTRO + """
## Rules
- [Tea](tea.md) — likes tea · [Build](build.md) — make test
- Ask before deleting anything
### Tools
- [Lint](lint.md) — ruff, then mypy · plain note
Some paragraph that is not a list line.

## Projects
- [Old](old.md) — an old project
"""


def test_every_original_segment_stays_in_l0(tmp_path: Path) -> None:
    root = _store(tmp_path, SECTIONED, "tea.md", "build.md", "lint.md", "old.md", "never-listed.md")
    res = run_migrate(str(root), apply=True)
    assert res.code == 0, res.messages
    l0 = (root / L0).read_text(encoding="utf-8")
    assert all(normalize_ws(seg) in _pieces(l0) for seg in _segments(SECTIONED))
    assert "never-listed.md" not in l0 and "[never-listed](never-listed.md)" in (root / L1).read_text(encoding="utf-8")
    assert res.stats["resident"] == 4 and res.stats["manual"] == 2 and res.stats["untouched"] == 1
    assert (res.stats["notes"], res.stats["notes_file"]) == (1, "index-notes.md")
    notes = (root / "index-notes.md").read_text(encoding="utf-8")
    assert notes.endswith("\n## Rules\n\n### Tools\nSome paragraph that is not a list line.\n")
    assert _meta(root, "lint.md") == {"tier": "resident", "idx": "[Lint](lint.md) — ruff, then mypy",
                                      "idx_section": "Rules", "idx_sub": "Tools", "idx_order": "40"}
    assert "tier" not in _meta(root, "never-listed.md")


def test_separator_inside_brackets_is_not_split(tmp_path: Path) -> None:
    index = "## S\n- [A · B](a.md) — x (y · z) · [C](c.md) — w\n"
    assert _segments(index) == ["[A · B](a.md) — x (y · z)", "[C](c.md) — w"]
    root = _store(tmp_path, index, "a.md", "c.md")
    assert run_migrate(str(root), apply=True).code == 0
    assert _meta(root, "a.md")["idx"] == "[A · B](a.md) — x (y · z)"
    assert {"[A · B](a.md) — x (y · z)", "[C](c.md) — w"} <= _pieces((root / L0).read_text(encoding="utf-8"))


def test_second_segment_for_the_same_file_becomes_a_manual_line(tmp_path: Path) -> None:
    index = "## S\n- [A](a.md) — first · [A again](a.md) — second\n- [A](a.md) — third\n"
    root = _store(tmp_path, index, "a.md")
    res = run_migrate(str(root), apply=True)
    assert res.code == 0, res.messages
    assert _meta(root, "a.md")["idx"] == "[A](a.md) — first"
    lines = json.loads((root / MANUAL).read_text(encoding="utf-8"))["lines"]
    assert [line["seg"] for line in lines] == ["[A again](a.md) — second", "[A](a.md) — third"]
    assert set(_segments(index)) <= _pieces((root / L0).read_text(encoding="utf-8"))
    assert sum("an earlier line already links a.md" in m for m in res.messages) == 2


def test_section_names_with_slashes_migrate_idempotently(tmp_path: Path) -> None:
    index = "## Tools / CLI\n- [A](a.md) — x\n### a/b/c\n- [B](b.md) — y\n- free/text\n"
    root = _store(tmp_path, index, "a.md", "b.md")
    assert run_migrate(str(root), apply=True).code == 0
    after = tree(root)
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and "already uses" in res.messages[0] and tree(root) == after
    assert _meta(root, "b.md")["idx_section"] == "Tools / CLI" and _meta(root, "b.md")["idx_sub"] == "a/b/c"
    assert "## Tools / CLI\n" in (root / L0).read_text(encoding="utf-8") and "### a/b/c\n" in (root / L0).read_text(encoding="utf-8")


def test_all_resident_store_still_writes_l1(tmp_path: Path) -> None:
    root = _store(tmp_path, "## S\n- [A](a.md) — x\n- [B](b.md) — y\n", "a.md", "b.md")
    assert run_migrate(str(root), apply=True).code == 0
    assert link_line(L1) in (root / L0).read_text(encoding="utf-8") and (root / L1).is_file()
    assert "- [" not in (root / L1).read_text(encoding="utf-8")


def test_crlf_or_bom_index_migrates_like_lf(tmp_path: Path) -> None:
    names = ("tea.md", "build.md", "lint.md", "old.md")
    lf = _store(tmp_path / "lf", SECTIONED, *names)
    others = [_store(tmp_path / "crlf", SECTIONED, *names, eol="\r\n"),
              _store(tmp_path / "bom", "\ufeff" + SECTIONED, *names)]
    for root in [lf] + others:
        assert run_migrate(str(root), apply=True).code == 0
    for root in others:
        assert all((lf / name).read_bytes() == (root / name).read_bytes() for name in (L0, L1, MANUAL) + names)


def test_split_index_edge_cases() -> None:
    text = ("---\ntags:\n- not a list item\n---\n# T\n\n- first\n  - nested · two\n##no space\n### Sub\n\n## \n"
            "- under an empty heading\n- \n* star\n")
    index = split_index(text)
    assert index.preamble == "---\ntags:\n- not a list item\n---\n# T"
    assert [(s.text, s.section, s.sub, s.order, s.line) for s in index.segments] == [
        ("first", "Other", "", 10, 7), ("under an empty heading", "Other", "", 20, 13)]
    assert index.notes.lines == ["  - nested · two", "##no space", "- ", "* star"]
    assert index.notes.body == ["", "  - nested · two", "##no space", "", "## ", "- ", "* star"]
    empty = split_index("\n\n")
    assert (empty.preamble, empty.segments, empty.notes.lines) == (None, [], [])


def test_over_budget_index_fits_and_overflows_to_l1(tmp_path: Path) -> None:
    root = make_store(tmp_path)
    rows = []
    for n in range(300):
        write_memory(root, f"m{n:03d}.md", name=f"m{n:03d}")
        rows.append(f"- [Memory {n:03d}](m{n:03d}.md) — " + "detail " * 12)
    index = "# Index\n\n## All\n" + "\n".join(rows) + "\n"
    write_index(root, index)
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and "suggest" in " ".join(res.messages)
    l0, l1 = (root / L0).read_bytes(), (root / L1).read_text(encoding="utf-8")
    assert len(l0) <= 20480 and l0.count(b"\n") <= 160 and "## Overflow" in l1
    assert set(map(normalize_ws, _segments(index))) <= _pieces(l0.decode()) | _pieces(l1)
    assert all(_meta(root, f"m{n:03d}.md")["tier"] == "resident" for n in range(300))


def test_dry_run_writes_nothing_and_predicts_the_result(tmp_path: Path) -> None:
    root = _store(tmp_path, SECTIONED, "tea.md", "build.md", "lint.md", "old.md")
    before = snapshot(root)
    res = run_migrate(str(root))
    assert res.code == 0 and snapshot(root) == before and not (root / ".index.lock").exists()
    stats = dict(res.stats)
    lines = summary(res)
    assert lines[0].startswith("migrate (dry run): 4 memory files resident, 2 manual lines") and "--apply" in lines[3]
    assert lines[2] == ("1 line(s) that are not index lines (code blocks, nested lists, paragraphs) would move"
                        " verbatim to index-notes.md")
    applied = run_migrate(str(root), apply=True)
    assert applied.code == 0
    assert stats["l0_bytes"] == len((root / L0).read_bytes()) == applied.stats["l0_bytes"]
    assert summary(applied)[2].endswith("moved verbatim to index-notes.md")
    assert summary(applied)[3].startswith(f"snapshot {applied.stats['snapshot']}: undo with")


def test_flat_list_without_headings(tmp_path: Path) -> None:
    index = "# Memory Index\n\n- [A](a.md) — x\n- [B](b.md) — y\n"
    root = _store(tmp_path, index, "a.md", "b.md")
    assert run_migrate(str(root), apply=True).code == 0
    assert json.loads((root / MANUAL).read_text(encoding="utf-8"))["preamble"] == "# Memory Index"
    assert _meta(root, "a.md")["idx_section"] == "Other"
    assert "## Other\n- [A](a.md) — x\n- [B](b.md) — y\n" in (root / L0).read_text(encoding="utf-8")


def test_already_migrated_store_is_left_unchanged(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    before = tree(root)
    for apply in (False, True):
        res = run_migrate(str(root), apply=apply)
        assert res.code == 0 and "already uses" in res.messages[0] and tree(root) == before
    assert summary(res) == ["this store already uses the generated index: nothing to migrate"]


def test_tiered_store_never_built_is_built_by_apply_only(tmp_path: Path) -> None:
    root = make_store(tmp_path)
    write_memory(root, "a.md", name="a", tier="resident", idx="[A](a.md) — x")
    before = snapshot(root)
    res = run_migrate(str(root))
    assert res.code == 0 and "never built" in res.messages[0] and snapshot(root) == before
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and "[A](a.md) — x" in (root / L0).read_text(encoding="utf-8")
    assert not any(p.name.startswith(".index-backup-") for p in root.iterdir())


def test_unusable_links_become_manual_lines(tmp_path: Path, symlink: Callable[[Path, Path], None]) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(memory_text(name="outside"), encoding="utf-8")
    index = ("## S\n- [OK](ok.md) — fine\n- [Gone](gone.md) — missing · [Out](../outside.md) — outside\n"
             "- [L](linked.md) — link\n- [Two](a.md) and [links](b.md)\n- [Web](https://example.com/x.md)\n")
    root = _store(tmp_path, "", "ok.md", "a.md", "b.md")
    write_index(root, index)
    symlink(root / "linked.md", outside)
    before = outside.read_bytes(), os.stat(outside).st_mtime_ns
    res = run_migrate(str(root), apply=True)
    assert res.code == 7 and "skipped linked.md" in res.messages[-1], res.messages
    assert (outside.read_bytes(), os.stat(outside).st_mtime_ns) == before and (root / "linked.md").is_symlink()
    assert [line["seg"] for line in json.loads((root / MANUAL).read_text(encoding="utf-8"))["lines"]] == _segments(index)[1:]
    notes = " ".join(res.messages)
    assert "'gone.md' is not a readable" in notes and "'../outside.md' is not a readable" in notes
    assert "tier" not in _meta(root, "a.md") and set(_segments(index)) <= _pieces((root / L0).read_text(encoding="utf-8"))
    assert not any("outside" in rel for rel in tree(root) if rel.startswith(".index-backup-"))


def test_index_without_memory_links(tmp_path: Path) -> None:
    index = "# Notes\n\n## Rules\n- Ask before deleting anything · Prefer small commits\n"
    root = _store(tmp_path, index, "a.md")
    res = run_migrate(str(root), apply=True)
    assert res.code == 0, res.messages
    assert "## Rules\n- Ask before deleting anything\n- Prefer small commits\n" in (root / L0).read_text(encoding="utf-8")
    assert run_migrate(str(root), apply=True).code == 0


def test_sources_that_cannot_take_the_line(tmp_path: Path) -> None:
    index = ("## S\n- [D](diff.md) — new text\n- [E](equal.md) — same text\n- [U](unclosed.md) — u\n"
             "- [H](hard.md) — h\n")
    root = make_store(tmp_path)
    write_memory(root, "diff.md", idx="the file's own line")
    write_memory(root, "equal.md", idx="[E](equal.md) —  same text", idx_order=7)
    (root / "unclosed.md").write_text("---\nname: never closed\n", encoding="utf-8")
    write_memory(root, "hard.md", name="hard")
    os.link(root / "hard.md", tmp_path / "hard-twin.md")
    write_index(root, index)
    res = run_migrate(str(root), apply=True)
    assert res.code == 7, res.messages
    assert _meta(root, "diff.md") == {"idx": "the file's own line"}
    assert _meta(root, "equal.md") == {"idx": "[E](equal.md) —  same text", "idx_order": "7", "tier": "resident",
                                       "idx_section": "S", "idx_sub": ""}
    assert (root / "unclosed.md").read_text(encoding="utf-8") == "---\nname: never closed\n" and "tier" not in _meta(root, "hard.md")
    notes = " ".join(res.messages)
    assert "different idx" in notes and "cannot be patched" in notes and "hard link" in notes


def test_index_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    root = _store(tmp_path, "", "a.md")
    (root / L0).write_bytes(b"## S\n- [A](a.md) \xff\n")
    before = snapshot(root)
    for apply in (False, True):
        res = run_migrate(str(root), apply=apply)
        assert res.code == 2 and "UTF-8" in res.messages[-1]
    assert snapshot(root) == before


@pytest.mark.parametrize("name, content", [(".memory-index.json", '{"bogus": 1}'), (MANUAL, "{")])
def test_configuration_errors_exit_2(tmp_path: Path, name: str, content: str) -> None:
    root = _store(tmp_path, "## S\n- [A](a.md) — x\n", "a.md")
    (root / name).write_text(content, encoding="utf-8")
    assert run_migrate(str(root), apply=True).code == 2 and "tier" not in _meta(root, "a.md")
