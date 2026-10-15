"""DESIGN §8: text of a hand-written MEMORY.md that is not an index line moves verbatim to a notes file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from storekit import make_store, snapshot, tree, write_index, write_memory
from test_migrate_apply import record_replaces

from cc_memory_index.build import run_check
from cc_memory_index.frontmatter import parse
from cc_memory_index.migrate import run_migrate, summary
from cc_memory_index.migrate_plan import split_index
from cc_memory_index.migrate_snapshot import SNAPSHOT_PREFIX
from cc_memory_index.render import link_line
from cc_memory_index.text import normalize_ws, split_segments

L0, L1, NOTES, MIGRATING = "MEMORY.md", "index-l1.md", "index-notes.md", ".index-migrating"
INDEX = """# Project memory
> Read this first.

## Rules
- [Tea](tea.md) — likes tea · [Build](build.md) — make test
  - nested under build: run it twice
    - deeper still
- Ask before deleting anything

```sh
- make test   # a list line inside code
## not a heading either

```
A paragraph that explains the rules,
over two lines.

| tool | use |
|---|---|
| ruff | lint |
### Tools
- [Lint](lint.md) — ruff, then mypy
> a quote after a heading
<!--
- [Old](old.md) — commented out
-->
## Projects
- [Old](old.md) — an old project
"""
NOTES_BODY = """
## Rules
  - nested under build: run it twice
    - deeper still

```sh
- make test   # a list line inside code
## not a heading either

```
A paragraph that explains the rules,
over two lines.

| tool | use |
|---|---|
| ruff | lint |

### Tools
> a quote after a heading
<!--
- [Old](old.md) — commented out
-->
"""
NOTE_LINES = ["  - nested under build: run it twice", "    - deeper still", "```sh",
              "- make test   # a list line inside code", "## not a heading either", "", "```",
              "A paragraph that explains the rules,", "over two lines.", "| tool | use |", "|---|---|",
              "| ruff | lint |", "> a quote after a heading", "<!--", "- [Old](old.md) — commented out", "-->"]
WHAT = "code blocks, nested lists and paragraphs that were not index lines"
NOTES_TEXT = f"""---
name: index-notes
description: Text moved out of MEMORY.md by cc-memory-index migrate — {WHAT}
metadata:
  type: reference
  tier: resident
  idx: "[Notes moved from MEMORY.md](index-notes.md) — {WHAT}"
  idx_section: ""
  idx_sub: ""
  idx_order: 0
---
# Notes moved from MEMORY.md
{NOTES_BODY}"""
NAMES = ("tea.md", "build.md", "lint.md", "old.md")


def _store(base: Path, index: str = INDEX, eol: str = "\n") -> Path:
    base.mkdir(parents=True, exist_ok=True)
    root = make_store(base)
    for name in NAMES:
        write_memory(root, name, name=name[:-3], description=f"about {name[:-3]}")
    write_index(root, index, eol)
    return root


def _top(root: Path) -> dict[str, bytes]:
    return {name: data for name, data in tree(root).items() if not name.startswith(SNAPSHOT_PREFIX)}


def _ident(root: Path) -> str:
    return next(p.name for p in root.iterdir() if p.name.startswith(SNAPSHOT_PREFIX))[len(SNAPSHOT_PREFIX):]


def _index_pieces(text: str) -> set[str]:
    return {normalize_ws(p) for line in text.split("\n") if line.startswith("- ") for p in split_segments(line[2:])}


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_notes_move_verbatim_and_in_order(tmp_path: Path, eol: str) -> None:
    root = _store(tmp_path, eol=eol)
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and (res.stats["notes"], res.stats["notes_file"]) == (16, NOTES), res.messages
    assert (root / NOTES).read_bytes() == NOTES_TEXT.encode("utf-8")
    assert split_index(INDEX).notes.lines == NOTE_LINES
    original = (root / (SNAPSHOT_PREFIX + _ident(root)) / L0).read_bytes().split(eol.encode())
    for lines in (original, (root / NOTES).read_bytes().split(b"\n")):
        left = iter(lines)
        assert all(any(line == note.encode("utf-8") for line in left) for note in NOTE_LINES)
    l0 = (root / L0).read_text(encoding="utf-8")
    body = l0.split(link_line(L1))[1].split("\n")
    assert not any(line.lstrip().startswith(("```", "|", "<!--", ">")) for line in body)
    assert "make test   #" not in l0 and "commented out" not in l0 and "deeper still" not in l0
    assert {seg.text for seg in split_index(INDEX).segments} <= _index_pieces(l0)
    assert f"- [Notes moved from MEMORY.md]({NOTES}) — {WHAT}\n" in l0
    assert res.stats["manual"] == 1 and (res.stats["build"]["rescued"], res.stats["build"]["manual_added"]) == ([], [])
    assert run_check(str(root)).code == 0 and run_migrate(str(root), apply=True).code == 0


def test_crlf_and_lf_give_the_same_files(tmp_path: Path) -> None:
    lf, crlf = _store(tmp_path / "lf"), _store(tmp_path / "crlf", eol="\r\n")
    assert run_migrate(str(lf), apply=True).code == run_migrate(str(crlf), apply=True).code == 0
    assert all((lf / name).read_bytes() == (crlf / name).read_bytes() for name in (L0, L1, NOTES) + NAMES)


def test_dry_run_reports_the_notes_and_writes_nothing(tmp_path: Path) -> None:
    root = _store(tmp_path)
    before = snapshot(root)
    res = run_migrate(str(root))
    assert res.code == 0 and snapshot(root) == before
    assert ("16 line(s) that are not index lines (code blocks, nested lists, paragraphs) would move verbatim to"
            f" {NOTES}") in summary(res)
    applied = run_migrate(str(root), apply=True)
    assert res.stats["l0_bytes"] == len((root / L0).read_bytes()) == applied.stats["l0_bytes"]


def test_restore_round_trip_removes_the_notes_file(tmp_path: Path) -> None:
    root = _store(tmp_path)
    original = _top(root)
    assert run_migrate(str(root), apply=True).code == 0 and (root / NOTES).is_file()
    res = run_migrate(str(root), restore=_ident(root))
    assert res.code == 0 and NOTES in res.stats["restored"], res.messages
    assert _top(root) == original


def test_restore_leaves_an_edited_notes_file(tmp_path: Path) -> None:
    root = _store(tmp_path)
    original = _top(root)
    assert run_migrate(str(root), apply=True).code == 0
    edited = (root / NOTES).read_text(encoding="utf-8") + "A line added after the migration.\n"
    (root / NOTES).write_text(edited, encoding="utf-8")
    res = run_migrate(str(root), restore=_ident(root))
    assert res.code == 4 and res.stats["conflicts"] == [NOTES], res.messages
    assert (root / NOTES).read_text(encoding="utf-8") == edited
    assert {n: d for n, d in _top(root).items() if n != NOTES} == original


def test_the_name_avoids_existing_files_and_names_the_index_links(tmp_path: Path) -> None:
    root = _store(tmp_path, INDEX + "- [Later](index-notes-2.md) — not written yet\n")
    write_memory(root, NOTES, name="index-notes", description="my own notes")
    mine = (root / NOTES).read_bytes()
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and res.stats["notes_file"] == "index-notes-3.md", res.messages
    assert (root / NOTES).read_bytes() == mine and not (root / "index-notes-2.md").exists()
    text = (root / "index-notes-3.md").read_text(encoding="utf-8")
    assert parse(text).get("name") == "index-notes-3" and "(index-notes-3.md)" in (parse(text).get("idx") or "")
    manifest = json.loads((root / (SNAPSHOT_PREFIX + _ident(root)) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]["index-notes-3.md"]["before"] is None and NOTES in manifest["files"]


def test_an_index_of_index_lines_only_gets_no_notes_file(tmp_path: Path) -> None:
    root = _store(tmp_path, "## Rules\n- [Tea](tea.md) — likes tea\n\n- Ask first\n")
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and (res.stats["notes"], res.stats["notes_file"]) == (0, None)
    assert not any(p.name.startswith("index-notes") for p in root.iterdir())
    assert not any("verbatim" in line for line in summary(res))


def _crash_after_notes(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as m:
        calls = record_replaces(m, crash_at=10)
        assert run_migrate(str(root), apply=True).code == 6 and calls[-2:] == [NOTES, "build.md"], calls


def test_a_note_removed_from_the_notes_file_keeps_the_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _store(tmp_path)
    _crash_after_notes(root, monkeypatch)
    text = (root / NOTES).read_text(encoding="utf-8")
    (root / NOTES).write_text(text.replace("over two lines.\n", ""), encoding="utf-8")
    res = run_migrate(str(root), apply=True)
    assert res.code == 1 and "'over two lines.' would not be in index-notes.md" in res.messages[-1], res.messages
    assert (root / MIGRATING).is_file() and "--restore" in res.messages[-1]


def test_a_line_added_during_an_interrupted_migration_is_rescued_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _store(tmp_path)
    _crash_after_notes(root, monkeypatch)
    write_index(root, INDEX + "- Added while the migration was interrupted\n")
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and res.stats["build"]["manual_added"] == ["Added while the migration was interrupted"]
    assert res.stats["build"]["rescued"] == [] and run_check(str(root)).code == 0
    assert (root / NOTES).read_bytes() == NOTES_TEXT.encode("utf-8")


def test_more_notes_than_a_memory_file_may_hold_are_refused(tmp_path: Path) -> None:
    root = _store(tmp_path, "## S\n- [Tea](tea.md) — x\n" + ("p" * 1023 + "\n") * 1025)
    before = snapshot(root)
    for apply in (False, True):
        res = run_migrate(str(root), apply=apply)
        assert res.code == 2 and "1024 KiB" in res.messages[-1] and snapshot(root) == before


@pytest.mark.parametrize("text, notes", [
    ("## S\n~~~\n- in tilde code\n~~~~\n- item\n", ["~~~", "- in tilde code", "~~~~"]),
    ("## S\n```\n- never closed\n## still code\n", ["```", "- never closed", "## still code"]),
    ("## S\n```inline``` is no fence\n- item\n", ["```inline``` is no fence"]),
    ("## S\n<!-- one line -->\n- item\n  ```\n  - indented code\n  ```\n", [
        "<!-- one line -->", "  ```", "  - indented code", "  ```"]),
    ("# T\n```\n- in preamble code\n```\n## S\n- item\n", []),
])
def test_code_blocks_and_comments_hold_no_index_lines(text: str, notes: list[str], tmp_path: Path) -> None:
    index = split_index(text)
    assert index.notes.lines == notes
    assert [seg.text for seg in index.segments] == (["item"] if "- item" in text else [])
    assert index.preamble == ("# T\n```\n- in preamble code\n```" if text.startswith("# T") else None)
