from __future__ import annotations

import time
from pathlib import Path

import pytest
from storekit import append, built_store, make_store, write_index, write_memory

from cc_memory_index.build import run_build
from cc_memory_index.frontmatter import parse
from cc_memory_index.links import LINE_MAX_CHARS, memory_links
from cc_memory_index.lint import run_lint
from cc_memory_index.migrate import run_migrate

L0, L1 = "MEMORY.md", "index-l1.md"


@pytest.mark.parametrize("text, links", [
    ("[a](a.md) [web](https://x.org/b.md) [mail](mailto:d.md) [up](../e.md) [f]( f.md)", ["a.md", "../e.md"]),
    ("[N](n.md#top) [frag](#top) [both](<b.md#x>) [angle](<c.md>)", ["n.md", "b.md", "c.md"]),
    ("[x](x.MD) [t](t.md \"title\") [n](n.md)x [[w]](w.md) [q](q.md?x)", ["n.md"]),
    ("[a\nb](a.md) [c](c.md)\n[d](d.md) [d](d.md)", ["c.md", "d.md", "d.md"]),
    ("[C:](C:x.md) [](e.md)", ["e.md"]),
])
def test_one_rule_for_links(text: str, links: list[str]) -> None:
    assert memory_links(text) == links


# Short ids: pytest puts the test id in an environment variable, which Windows caps at 32,767 characters.
@pytest.mark.parametrize("line", [
    "[" * 400_000, "[a](" * 100_000, "[a]([" * 80_000, "](" * 200_000, "[" + "x" * 400_000, "[x]" * 130_000,
], ids=["brackets", "open-targets", "nested-open", "close-open", "open-text", "bare-labels"])
def test_link_extraction_is_linear(line: str) -> None:
    start = time.perf_counter()
    memory_links("- " + line + "\n" + line)
    assert time.perf_counter() - start < 1.0


def test_each_line_is_read_up_to_its_cap() -> None:
    assert memory_links("x" * LINE_MAX_CHARS + "[a](a.md)\n[b](b.md)") == ["b.md"]


def _dir(base: Path, name: str) -> Path:
    (base / name).mkdir()
    return base / name


def _quick(action: object) -> float:
    start = time.perf_counter()
    action()  # type: ignore[operator]
    return time.perf_counter() - start


def test_a_long_run_of_brackets_does_not_stall_build_or_migrate(tmp_path: Path) -> None:
    """Audit PoC 6: one line of 400 KiB '[' took 55 s in the hook's build and 109 s in a dry run."""
    line = "- " + "[" * (400 * 1024) + "\n"
    root = built_store(_dir(tmp_path, "built"))
    append(root, L0, line)
    assert _quick(lambda: run_build(str(root), False)) < 1.0
    hand = make_store(_dir(tmp_path, "hand"))
    write_memory(hand, "a.md", name="a")
    write_index(hand, "# Memory\n\n" + line)
    assert _quick(lambda: run_migrate(str(hand))) < 1.0


def test_rescue_migrate_and_lint_agree_on_a_fragment_link(tmp_path: Path) -> None:
    hand = make_store(_dir(tmp_path, "hand"))
    write_memory(hand, "n.md", name="n")
    write_index(hand, "## S\n- [N](n.md#top) — with a fragment\n")
    assert run_migrate(str(hand), apply=True).code == 0
    assert parse((hand / "n.md").read_text(encoding="utf-8")).get("idx") == "[N](n.md#top) — with a fragment"
    assert run_lint(str(hand)).code == 0
    root = built_store(_dir(tmp_path, "built"))
    write_memory(root, "m.md", name="m")
    append(root, L0, "- [M](m.md#part) — added by hand\n")
    res = run_build(str(root), False)
    assert res.code == 0 and res.stats["rescued"] == [{"file": "m.md", "idx": "[M](m.md#part) — added by hand"}]
    write_memory(root, "z.md", name="z", tier="resident", idx="[gone](gone.md#x) — dangling")
    run_build(str(root), False)
    assert "error: MEMORY.md links to gone.md, which does not exist" in run_lint(str(root)).messages
