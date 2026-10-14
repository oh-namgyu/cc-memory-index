from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest
from storekit import make_store, memory_text, snapshot, write_json, write_memory

from cc_memory_index import store as store_mod
from cc_memory_index.build import run_build
from cc_memory_index.items import PENDING_NAME
from cc_memory_index.lint import run_lint

Symlink = Callable[[Path, Path], None]
L0, L1, MARK = "MEMORY.md", "index-l1.md", ".index-render.json"


def clean_store(tmp_path: Path) -> Path:
    root = make_store(tmp_path)
    write_memory(root, "build.md", name="build", description="make test", type="reference", tier="resident")
    (root / "tea.md").write_text(memory_text("see [[build]] and [[Tea time]]\n", name="Tea time", type="user"))
    return root


def _lint(root: Path) -> tuple[int, list[str], list[str]]:
    res = run_lint(str(root))
    errors = [m[len("error: "):] for m in res.messages if m.startswith("error: ")]
    warnings = [m[len("warning: "):] for m in res.messages if m.startswith("warning: ")]
    assert len(errors) + len(warnings) == len(res.messages)
    assert (res.stats["errors"], res.stats["warnings"]) == (len(errors), len(warnings))
    return res.code, errors, warnings


def _built(root: Path) -> Path:
    assert run_build(str(root), False).code in (0, 1, 7)
    return root


def test_clean_store_has_no_messages_and_lint_writes_nothing(tmp_path: Path) -> None:
    root = _built(clean_store(tmp_path))
    (root / ".index.lock").unlink()
    before = snapshot(root)
    code, errors, warnings = _lint(root)
    assert (code, errors) == (0, [])
    assert warnings == ["tea.md: frontmatter name 'Tea time' differs from the file name"]
    assert snapshot(root) == before and not (root / ".index.lock").exists()


def test_unparseable_frontmatter_is_an_error(tmp_path: Path) -> None:
    root = clean_store(tmp_path)
    (root / "open.md").write_text("---\nname: open\nno closing line\n")
    code, errors, _ = _lint(_built(root))
    assert code == 1 and errors[0].startswith("open.md: frontmatter cannot be parsed")


@pytest.mark.parametrize("kind", ["link", "large", "binary", "cap"])
def test_skipped_sources_are_errors(
    tmp_path: Path, symlink: Symlink, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = _built(clean_store(tmp_path))
    if kind == "link":
        symlink(root / "zz.md", root / "build.md")
    elif kind == "large":
        (root / "zz.md").write_bytes(b"x" * (store_mod.SOURCE_MAX_BYTES + 1))
    elif kind == "binary":
        (root / "zz.md").write_bytes(b"---\nname: \xff\n---\n")
    else:
        write_memory(root, "zz.md", name="zz")
        _built(root)
        monkeypatch.setattr(store_mod, "MAX_SOURCES", 2)
    code, errors, _ = _lint(root)
    assert code == 1 and any(e.startswith("zz.md was skipped: ") for e in errors)


def test_dangling_link_in_the_index_is_an_error(tmp_path: Path) -> None:
    root = clean_store(tmp_path)
    write_memory(root, "note.md", name="note", tier="resident", idx="[Gone](gone.md) — see note.md")
    code, errors, _ = _lint(_built(root))
    assert (code, errors) == (1, [f"{L0} links to gone.md, which does not exist"])


def test_links_outside_the_store_are_warnings_and_urls_are_ignored(tmp_path: Path) -> None:
    root = clean_store(tmp_path)
    write_memory(root, "note.md", name="note", idx="[up](../x.md) · [web](https://example.com/a.md) — note")
    code, errors, warnings = _lint(_built(root))
    assert (code, errors) == (0, [])
    assert f"{L1} links to '../x.md', which is not a file in the store" in warnings


def test_new_source_is_unreachable_until_built(tmp_path: Path) -> None:
    root = _built(clean_store(tmp_path))
    write_memory(root, "fresh.md", name="fresh", description="added after the build")
    code, errors, _ = _lint(root)
    assert code == 1
    assert f"fresh.md is not listed in {L0} or {L1}: the index is stale; run `cc-memory-index build`" in errors
    assert f"{L1} is out of date: run `cc-memory-index build`" in errors
    _built(root)
    assert _lint(root)[:2] == (0, [])


def test_changed_idx_makes_the_index_out_of_sync(tmp_path: Path) -> None:
    root = _built(clean_store(tmp_path))
    write_memory(root, "build.md", name="build", description="make check", type="reference", tier="resident")
    code, errors, _ = _lint(root)
    assert (code, errors) == (1, [f"{L0} is out of date: run `cc-memory-index build`"])


@pytest.mark.parametrize("file, why", [("build.md", "has a different idx"), ("gone.md", "is missing")])
def test_unresolved_pending_entry_is_an_error(tmp_path: Path, file: str, why: str) -> None:
    root = clean_store(tmp_path)
    write_memory(root, "build.md", name="build", tier="resident", idx="[build](build.md) — make test")
    entry = {"file": file, "idx": f"rescued line for {file}", "section": "", "sub": "", "order": 0}
    write_json(root, PENDING_NAME, {"version": 1, "entries": [entry]})
    code, errors, _ = _lint(_built(root))
    assert code == 1 and len(errors) == 1
    assert errors[0].startswith(f"{PENDING_NAME}: the line for {file} is unresolved (its memory file {why})")


def test_wiki_links_exact_variant_and_missing(tmp_path: Path) -> None:
    root = clean_store(tmp_path)
    write_memory(root, "build-cmds.md", name="build-cmds")
    body = "[[build-cmds]] [[build-cmds.md|alias]] [[Build_Cmds]] [[nowhere#part]] [[nowhere]]\n"
    (root / "links.md").write_text(memory_text(body, name="links", idx="links [[ignored-in-frontmatter]]"))
    code, errors, warnings = _lint(_built(root))
    assert (code, errors) == (1, ["links.md: wiki link 'nowhere' does not resolve to any memory file"])
    assert "links.md: wiki link 'Build_Cmds' matches build-cmds.md only when case and '-'/'_' are ignored" in warnings


def test_warnings_for_names_oversized_idx_bad_names_and_no_frontmatter(tmp_path: Path) -> None:
    root = clean_store(tmp_path)
    write_memory(root, "long.md", name="long", tier="resident", idx="[long](long.md) — " + "y" * 320)
    (root / "plain.md").write_text("just text, no frontmatter\n")
    (root / "_bad.md").write_text("x\n")
    (root / "a..b.md").write_text("x\n")
    code, errors, warnings = _lint(_built(root))
    assert (code, errors) == (0, [])
    assert sorted(w.split(":")[0].split(" ")[0] for w in warnings) == ["'_bad.md'", "'a..b.md'", "long.md",
                                                                        "plain.md", "tea.md"]
    assert any("over item_max_bytes (300)" in w and "Overflow" in w for w in warnings)
    assert "plain.md has no frontmatter: it is indexed by its file name only" in warnings


def test_stale_previous_l1_file_is_a_warning(tmp_path: Path) -> None:
    root = _built(clean_store(tmp_path))
    record = json.loads((root / MARK).read_text())
    record["l1_name"] = "old-l1.md"
    (root / MARK).write_text(json.dumps(record))
    (root / "old-l1.md").write_text(memory_text(name="old-l1"))
    _, _, warnings = _lint(root)
    assert f"old-l1.md is the previous L1 file (l1_name is now {L1}): delete it once nothing links to it" in warnings


def test_record_naming_the_current_l1_is_fine(tmp_path: Path) -> None:
    root = _built(clean_store(tmp_path))
    record = json.loads((root / MARK).read_text())
    record["l1_name"] = L1
    (root / MARK).write_text(json.dumps(record))
    assert not any("previous L1" in w for w in _lint(root)[2])


def test_interrupted_migration_and_unmigrated_store_are_errors(tmp_path: Path) -> None:
    root = _built(clean_store(tmp_path))
    (root / ".index-migrating").write_text("")
    code, errors, _ = _lint(root)
    assert code == 1 and "migrate --apply" in errors[0] and len(errors) == 1
    (tmp_path / "hand").mkdir()
    hand = make_store(tmp_path / "hand")
    write_memory(hand, "a.md", name="a")
    (hand / L0).write_text("- [a](a.md) — written by hand\n")
    code, errors, _ = _lint(hand)
    assert code == 1 and errors == [f"{L0} was written by hand: run `cc-memory-index migrate` first"]


def test_bad_configuration_exits_2(tmp_path: Path) -> None:
    root = make_store(tmp_path, {"budget_bytes": 1})
    res = run_lint(str(root))
    assert res.code == 2 and "budget_bytes" in res.messages[0]
