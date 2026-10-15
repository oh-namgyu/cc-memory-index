"""DESIGN §6.1-§6.2 and the decisions around them: folding failures, the invariant, l1_name changes
and section names that collide with L1's own headings."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest
from storekit import append, built_store, claude_adds_memory, make_store, snapshot, write_json, write_memory

from cc_memory_index import fold as fold_mod
from cc_memory_index.build import run_build, run_check
from cc_memory_index.frontmatter import parse
from cc_memory_index.items import MANUAL_NAME, PENDING_NAME, Manual, ManualLine, load_manual
from cc_memory_index.safeio import StoreDir

L0, L1, MARK, CONFIG = "MEMORY.md", "index-l1.md", ".index-render.json", ".memory-index.json"


def _pending(root: Path) -> list[dict[str, object]]:
    return json.loads((root / PENDING_NAME).read_text(encoding="utf-8"))["entries"]


def _manual_lines(root: Path) -> list[ManualLine]:
    with StoreDir(str(root)) as sd:
        return load_manual(sd).lines


def _settled(root: Path) -> None:
    assert run_check(str(root)).code == 0
    res = run_build(str(root), False)
    assert (res.code, res.stats["written"]) == (0, [])


def test_unpatchable_target_stays_pending_and_listed(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    (root / "odd.md").write_text("---\nname: Odd\nmetadata: [a, b]\n---\nbody\n", encoding="utf-8")
    original = (root / "odd.md").read_bytes()
    append(root, L0, "- [Odd](odd.md) — kept anyway\n")
    res = run_build(str(root), False)
    entry = {"file": "odd.md", "idx": "[Odd](odd.md) — kept anyway", "section": "", "sub": "", "order": 1000001}
    assert res.code == 0 and res.stats["rescued"] == [{"file": "odd.md", "idx": entry["idx"]}]
    assert (root / "odd.md").read_bytes() == original and _pending(root) == [entry]
    assert any("'metadata' is not a block mapping" in m and PENDING_NAME in m for m in res.messages)
    assert "## Other\n- [Odd](odd.md) — kept anyway\n" in (root / L1).read_text(encoding="utf-8")
    assert run_check(str(root)).code == 0
    again = run_build(str(root), False)
    assert again.code == 0 and again.stats["written"] == [] and _pending(root) == [entry]
    (root / L1).write_text((root / L1).read_text(encoding="utf-8").replace("kept anyway", "kept anyway, edited"), encoding="utf-8")
    before = snapshot(root)
    refused = run_build(str(root), False)
    assert refused.code == 3 and "already has a line waiting" in refused.messages[0] and snapshot(root) == before


def test_target_edited_between_scan_and_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = built_store(tmp_path)
    line = claude_adds_memory(root)
    real = fold_mod.patch

    def racing(text: str, add: Mapping[str, Any], replace: Optional[Mapping[str, str]] = None) -> str:
        out = real(text, add, replace)
        append(root, "new_note.md", "written meanwhile\n")
        return out

    monkeypatch.setattr(fold_mod, "patch", racing)
    res = run_build(str(root), False)
    assert res.code == 4 and "new_note.md" in res.messages[-1] and "run again" in res.messages[-1]
    assert [e["file"] for e in _pending(root)] == ["new_note.md"]
    note = (root / "new_note.md").read_text(encoding="utf-8")
    assert parse(note).get("idx") is None and note.endswith("written meanwhile\n")
    assert line in (root / L1).read_text(encoding="utf-8") and line not in (root / L0).read_text(encoding="utf-8")
    monkeypatch.setattr(fold_mod, "patch", real)
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["written"] == ["new_note.md", PENDING_NAME]
    assert parse((root / "new_note.md").read_text(encoding="utf-8")).get("idx") == line[2:] and _pending(root) == []
    _settled(root)


def test_lost_line_stops_before_the_generated_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = built_store(tmp_path)
    append(root, L0, "- Ask before deleting anything\n")
    generated = {name: (root / name).read_bytes() for name in (L0, L1, MARK)}
    real = fold_mod.manual_bytes
    monkeypatch.setattr(fold_mod, "manual_bytes", lambda manual: real(Manual(manual.preamble, [])))
    res = run_build(str(root), False)
    assert res.code == 8 and "would be lost" in res.messages[-1] and "Ask before" in res.messages[-1]
    assert {name: (root / name).read_bytes() for name in (L0, L1, MARK)} == generated
    assert _manual_lines(root) == []
    monkeypatch.setattr(fold_mod, "manual_bytes", real)
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["manual_added"] == ["Ask before deleting anything"]
    _settled(root)


def test_line_added_to_an_l0_without_sections(tmp_path: Path) -> None:
    root = make_store(tmp_path)
    write_memory(root, "a.md", name="A", type="project")
    assert run_build(str(root), False).code == 0 and "\n## " not in (root / L0).read_text(encoding="utf-8")
    line = claude_adds_memory(root)
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["rescued"][0]["file"] == "new_note.md"
    assert parse((root / "new_note.md").read_text(encoding="utf-8")).get("idx_section") == ""
    assert "## Project\n- [A](a.md)\n" + line + "\n" in (root / L1).read_text(encoding="utf-8")
    _settled(root)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_untouched_old_l1_is_left_out_until_deleted(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    old = (root / L1).read_bytes()
    write_json(root, CONFIG, {"l1_name": "more.md"})
    res = run_build(str(root), False)
    stale = "stale generated file index-l1.md: delete it"
    assert res.code == 0 and any(m.startswith(stale) for m in res.messages)
    assert (root / L1).read_bytes() == old and L1 not in (root / "more.md").read_text(encoding="utf-8")
    assert "[more.md](more.md)" in (root / L0).read_text(encoding="utf-8")
    record = json.loads((root / MARK).read_text(encoding="utf-8"))
    assert record["l1_name"] == "more.md" and record["stale"] == {L1: _sha(old)}
    again = run_build(str(root), False)
    assert again.stats["written"] == [] and any(m.startswith(stale) for m in again.messages)
    assert run_check(str(root)).code == 0
    (root / L1).unlink()
    res = run_build(str(root), False)
    assert res.stats["written"] == [MARK] and not any("stale" in m for m in res.messages)
    assert "stale" not in json.loads((root / MARK).read_text(encoding="utf-8"))


def test_edited_old_l1_is_an_ordinary_source(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    append(root, L1, "- a note kept in the old file\n")
    write_json(root, CONFIG, {"l1_name": "more.md"})
    res = run_build(str(root), False)
    assert res.code == 0 and not any("stale" in m for m in res.messages)
    assert "- [index-l1](index-l1.md) — Generated index" in (root / "more.md").read_text(encoding="utf-8")
    assert "stale" not in json.loads((root / MARK).read_text(encoding="utf-8"))


def test_l0_edit_made_before_the_l1_rename_is_still_rescued(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    write_json(root, CONFIG, {"l1_name": "more.md"})
    line = claude_adds_memory(root)
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["rescued"][0]["file"] == "new_note.md"
    assert line in (root / "more.md").read_text(encoding="utf-8")


def test_sections_named_like_l1_headings_never_collide(tmp_path: Path) -> None:
    root = make_store(tmp_path)
    write_memory(root, "ov.md", idx="[ov](ov.md) — a section called Overflow", idx_section="Overflow")
    write_memory(root, "un.md", idx="[un](un.md) — one called unresolved (section)",
                 idx_section="unresolved (section)")
    write_memory(root, "c.md", idx="[c](c.md) — the file's own line")
    write_json(root, PENDING_NAME, {"version": 1, "entries": [
        {"file": "c.md", "idx": "[c](c.md) — a conflicting line", "section": "", "sub": "", "order": 0}]})
    assert run_build(str(root), False).code == 0
    l1 = (root / L1).read_text(encoding="utf-8")
    assert "\n## Overflow (section)\n- [ov](ov.md)" in l1 and "\n## unresolved (section) (section)\n" in l1
    assert l1.count("\n## Unresolved\n") == 1 and "\n## Overflow\n" not in l1
    l1 = l1.replace("## Overflow (section)\n", "## Overflow (section)\n- under the user section\n")
    l1 = l1.replace("## unresolved (section) (section)\n", "## unresolved (section) (section)\n- deeper\n")
    (root / L1).write_text(l1 + "- under the real Unresolved heading\n", encoding="utf-8")
    res = run_build(str(root), False)
    assert res.code == 0
    lines = [(m.seg, m.section) for m in _manual_lines(root)]
    assert lines == [("under the user section", "Overflow"), ("deeper", "unresolved (section)"),
                     ("under the real Unresolved heading", "Other")]
    l0 = (root / L0).read_text(encoding="utf-8")
    assert "## Overflow\n- under the user section\n" in l0 and "## Other\n- under the real" in l0
    _settled(root)


def test_manual_file_is_appended_to(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    write_json(root, MANUAL_NAME, {"version": 1, "preamble": None, "lines": [
        {"seg": "Existing rule", "section": "Rules", "sub": "", "order": 5}]})
    assert run_build(str(root), False).code == 0
    append(root, L0, "- New rule\n")
    assert run_build(str(root), False).code == 0
    assert [(m.seg, m.section, m.order) for m in _manual_lines(root)] == [
        ("Existing rule", "Rules", 5), ("New rule", "User", 1000001)]
