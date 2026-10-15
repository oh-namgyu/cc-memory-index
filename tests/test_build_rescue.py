"""DESIGN §6: a line added to a generated file by hand, one test per row of the table."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest
from storekit import append, built_store, claude_adds_memory, make_store, snapshot, write_memory

from cc_memory_index.build import run_build, run_check
from cc_memory_index.frontmatter import parse
from cc_memory_index.items import MANUAL_NAME, PENDING_NAME
from cc_memory_index.render import NOTE, TITLE, link_line

L0, L1 = "MEMORY.md", "index-l1.md"


def _pending(root: Path) -> list[dict[str, object]]:
    return json.loads((root / PENDING_NAME).read_text(encoding="utf-8"))["entries"]


def _manual(root: Path) -> list[dict[str, object]]:
    return json.loads((root / MANUAL_NAME).read_text(encoding="utf-8"))["lines"]


def _settled(root: Path) -> None:
    """The store is in sync and a rebuild writes nothing."""
    assert run_check(str(root)).code == 0
    res = run_build(str(root), False)
    assert (res.code, res.stats["written"]) == (0, [])


def test_line_for_a_new_memory_is_folded_into_its_frontmatter(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    line = claude_adds_memory(root)
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["rescued"] == [{"file": "new_note.md", "idx": line[2:]}]
    assert res.stats["manual_added"] == [] and res.stats["unrescuable"] == []
    fm = parse((root / "new_note.md").read_text(encoding="utf-8"))
    assert (fm.get("idx"), fm.get("idx_section"), fm.get("idx_sub"), fm.get("idx_order")) == (
        line[2:], None, None, "1000001")  # an empty section or subsection is not written
    assert _pending(root) == [] and not (root / MANUAL_NAME).exists()
    l1 = (root / L1).read_text(encoding="utf-8")
    assert line not in (root / L0).read_text(encoding="utf-8") and line + "\n" in l1
    assert l1.index("## Project") < l1.index(line)  # placed by its type, not where Claude appended it
    _settled(root)


def test_edited_default_line_becomes_the_files_idx(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    l1 = (root / L1).read_text(encoding="utf-8")
    (root / L1).write_text(l1.replace("an old project note", "an old project note, now archived"), encoding="utf-8")
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["rescued"][0]["file"] == "old.md"
    assert parse((root / "old.md").read_text(encoding="utf-8")).get("idx") == "[Old](old.md) — an old project note, now archived"
    assert "- [Old](old.md) — an old project note\n" not in (root / L1).read_text(encoding="utf-8")
    _settled(root)


@pytest.mark.parametrize("line", [
    "- Ask before deleting anything",
    "Ask before deleting anything",
    "- See [the guide](https://example.com/guide.md) first",
])
def test_line_without_a_memory_link_becomes_a_manual_line(tmp_path: Path, line: str) -> None:
    root = built_store(tmp_path)
    append(root, L0, line + "\n")
    res = run_build(str(root), False)
    seg = line[2:] if line.startswith("- ") else line
    assert res.code == 0 and res.stats["manual_added"] == [seg] and res.stats["rescued"] == []
    assert _manual(root) == [{"seg": seg, "section": "User", "sub": "", "order": 1000001}]
    assert f"## User\n- [Tea](tea.md) — likes tea\n- {seg}\n" in (root / L0).read_text(encoding="utf-8")
    assert not (root / PENDING_NAME).exists()
    _settled(root)


def test_manual_lines_keep_their_section_and_subsection(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    append(root, L0, "\n## Rules\n\n### Data\n\n- Back up first · Ask twice\n")
    assert run_build(str(root), False).code == 0
    assert [(m["seg"], m["section"], m["sub"], m["order"]) for m in _manual(root)] == [
        ("Back up first", "Rules", "Data", 1000001), ("Ask twice", "Rules", "Data", 1000002)]
    assert (root / L0).read_text(encoding="utf-8").endswith("## Rules\n\n### Data\n- Back up first\n- Ask twice\n")
    _settled(root)


def _with_rule(tmp_path: Path) -> Path:
    root = make_store(tmp_path)
    write_memory(root, "rule.md", tier="resident", idx="[Rule](rule.md) — always test", idx_section="Rules")
    write_memory(root, "tea.md", name="Tea", description="likes tea", type="user", tier="resident")
    assert run_build(str(root), False).code == 0
    return root


@pytest.mark.parametrize("edit, reason", [
    (lambda t: t + "- [Rule](rule.md) and [Tea](tea.md)\n", "links to 2 memory files"),
    (lambda t: t + "- [Gone](gone.md) — never written\n", "gone.md does not exist"),
    (lambda t: t + "- [Up](../secret.md) — outside\n", "is not a memory file in this directory"),
    (lambda t: t + "- [Index](index-l1.md) — the L1 file\n", "index-l1.md does not exist"),
    (lambda t: t.replace("always test", "always test twice"), "edited generated line"),
])
def test_unrescuable_line_writes_nothing(tmp_path: Path, edit: Callable[[str], str], reason: str) -> None:
    root = _with_rule(tmp_path)
    (root / L0).write_text(edit((root / L0).read_text(encoding="utf-8")), encoding="utf-8")
    before = snapshot(root)
    res = run_build(str(root), False)
    assert res.code == 3 and snapshot(root) == before
    (problem,) = res.stats["unrescuable"]
    assert reason in problem["reason"] and "edit the memory file's frontmatter" in problem["reason"]
    assert reason in res.messages[0] and "--force" in res.messages[-1]


@pytest.mark.parametrize("name, edit, reason", [
    (L0, lambda t: t.replace(NOTE, "> My own note."), "preamble edited — change it in .index-manual.json"),
    (L0, lambda t: "# Title\n" + t, "preamble edited"),
    (L1, lambda t: t.replace("generated: true", "generated: false"), "generated header was edited"),
    (L0, lambda t: t.replace("likes tea", "likes \udcff tea"), "not valid UTF-8"),
])
def test_edited_head_writes_nothing(tmp_path: Path, name: str, edit: Callable[[str], str], reason: str) -> None:
    root = _with_rule(tmp_path)
    append(root, L0, "- an addition that would be fine on its own\n")
    (root / name).write_bytes(edit((root / name).read_text(encoding="utf-8")).encode("utf-8", "surrogateescape"))
    before = snapshot(root)
    res = run_build(str(root), False)
    assert res.code == 3 and snapshot(root) == before
    assert reason in res.stats["unrescuable"][0]["reason"]


def test_deleted_and_reordered_generated_lines_are_restored(tmp_path: Path) -> None:
    root = _with_rule(tmp_path)
    fresh = (root / L0).read_text(encoding="utf-8")
    head, body = fresh.split("\n## ", 1)
    sections = body.split("\n## ")
    assert len(sections) == 2
    (root / L0).write_text(head + "\n## " + sections[1].rstrip("\n") + "\n\n## " + sections[0].split("\n")[0] + "\n", encoding="utf-8")
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["written"] == [L0] and (root / L0).read_text(encoding="utf-8") == fresh
    assert not (root / PENDING_NAME).exists() and not (root / MANUAL_NAME).exists()


def test_line_added_to_l1_is_rescued_too(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    write_memory(root, "new_note.md", name="New note", type="project")
    text = (root / L1).read_text(encoding="utf-8")
    (root / L1).write_text(text.replace("## Project\n", "## Project\n- [New note](new_note.md) — from L1\n"), encoding="utf-8")
    res = run_build(str(root), False)
    assert res.code == 0
    assert res.stats["rescued"] == [{"file": "new_note.md", "idx": "[New note](new_note.md) — from L1"}]
    assert parse((root / "new_note.md").read_text(encoding="utf-8")).get("idx_section") is None
    _settled(root)


def test_generated_lines_joined_or_moved_by_hand_are_not_an_edit_to_rescue(tmp_path: Path) -> None:
    root = make_store(tmp_path)
    for n in range(3):
        write_memory(root, f"m{n}.md", tier="resident", idx=f"[m{n}](m{n}.md) — short", idx_section="S")
    assert run_build(str(root), False).code == 0
    fresh = (root / L0).read_text(encoding="utf-8")
    lines = "".join(f"- [m{n}](m{n}.md) — short\n" for n in range(3))
    assert "\n## S\n" + lines in fresh
    joined = "- [m2](m2.md) — short\n\n- [m0](m0.md) —   short · [m1](m1.md) — short\n"
    (root / L0).write_text(fresh.replace(lines, joined), encoding="utf-8")
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["written"] == [L0] and (root / L0).read_text(encoding="utf-8") == fresh
    assert (res.stats["rescued"], res.stats["manual_added"]) == ([], [])


def test_generated_line_whose_idx_holds_separators_is_never_rescued(tmp_path: Path) -> None:
    """One item per line: a generated line is compared whole first, so pieces of an idx that holds
    ' · ' (here one linking to a memory without idx, one without any link) are never taken for lines
    added by hand, even when the line is copied with other whitespace around the separators."""
    root = make_store(tmp_path)
    idx = "[Hub](hub.md) — start here · [Spoke](spoke.md) — details · see both"
    write_memory(root, "hub.md", tier="resident", idx=idx, idx_section="S")
    write_memory(root, "spoke.md", name="Spoke", description="details", type="project")
    assert run_build(str(root), False).code == 0
    fresh = (root / L0).read_text(encoding="utf-8")
    assert f"\n## S\n- {idx}\n" in fresh
    copied = "- " + idx.replace(" · ", "\u00a0·\u3000")
    (root / L0).write_text(fresh + copied + "\n- " + idx + "\n", encoding="utf-8")
    res = run_build(str(root), False)
    assert res.code == 0 and (res.stats["rescued"], res.stats["manual_added"], res.stats["unrescuable"]) == ([], [], [])
    assert (root / L0).read_text(encoding="utf-8") == fresh and "idx" not in parse((root / "spoke.md").read_text(encoding="utf-8")).metadata
    append(root, L0, f"- {idx} · and a note added by hand\n")
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["manual_added"] == ["and a note added by hand"] and res.stats["rescued"] == []
    _settled(root)


def test_second_line_for_the_same_new_file_is_unrescuable(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    claude_adds_memory(root)
    append(root, L0, "- [New note](new_note.md) — a different line\n")
    before = snapshot(root)
    res = run_build(str(root), False)
    assert res.code == 3 and snapshot(root) == before
    assert [u["segment"] for u in res.stats["unrescuable"]] == ["[New note](new_note.md) — a different line"]
    assert "another added line already links to new_note.md" in res.messages[0]


def test_the_same_line_added_twice_is_kept_once(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    line = claude_adds_memory(root)
    append(root, L1, line + "\n")
    append(root, L0, "- free text\n- free text\n")
    res = run_build(str(root), False)
    assert res.code == 0 and len(res.stats["rescued"]) == 1 and res.stats["manual_added"] == ["free text"]
    _settled(root)


def test_deleted_head_lines_are_generated_and_come_back(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    fresh = (root / L0).read_text(encoding="utf-8")
    trimmed = fresh.replace(TITLE + "\n", "").replace(link_line(L1) + "\n", "")
    (root / L0).write_text(trimmed + "- Ask first\n", encoding="utf-8")
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["manual_added"] == ["Ask first"]
    l0 = (root / L0).read_text(encoding="utf-8")
    assert l0.startswith(TITLE + "\n") and link_line(L1) in l0 and "\n- Ask first\n" in l0
    _settled(root)
