from __future__ import annotations

from typing import Optional

import pytest
from storekit import item, source

from cc_memory_index.items import ManualLine, PendingEntry
from cc_memory_index.render import (
    L1_NOTE,
    NOTE,
    TITLE,
    head_lines,
    is_warning_line,
    l1_head,
    link_line,
    render,
)
from cc_memory_index.rescue import Context, Edited, analyse, default_pieces, lost, memory_links
from cc_memory_index.segments import Seg, body_segments, l1_heading, l1_section, pieces, split_head
from cc_memory_index.store import Config, Source

L1 = "index-l1.md"


@pytest.mark.parametrize("section, heading", [
    ("Notes", "Notes"), ("Overflow", "Overflow (section)"), ("unresolved", "unresolved (section)"),
    ("Overflow (section)", "Overflow (section) (section)"), ("Overflow  (section)", "Overflow  (section)"),
])
def test_l1_headings_round_trip(section: str, heading: str) -> None:
    assert l1_heading(section) == heading and l1_section(heading) == section


def test_l1_own_headings_have_no_section() -> None:
    assert l1_section("Overflow") is None and l1_section("UNRESOLVED") is None


def test_body_segments() -> None:
    lines = ["", "free line", "## A", "- a1 · [b](b.md) · c", "### Sub", "  - nested", "-", "#### deep",
             "## Overflow", "- spilled", "## B", "- [x · y](x.md) · z"]
    assert body_segments(lines, False) == [
        Seg("free line", "", ""), Seg("a1", "A", ""), Seg("[b](b.md)", "A", ""), Seg("c", "A", ""),
        Seg("nested", "A", "Sub"), Seg("#### deep", "A", "Sub"), Seg("spilled", "Overflow", ""),
        Seg("[x · y](x.md)", "B", ""), Seg("z", "B", ""),
    ]
    assert body_segments(["## Overflow", "### s", "- spilled", "## Overflow (section)", "- mine"], True) == [
        Seg("spilled", "", "s"), Seg("mine", "Overflow", "")]


def test_pieces_sanitise_before_splitting() -> None:
    assert pieces("a \x07· b") == ["a", "b"] and pieces("  x\t y ") == ["x y"] and pieces("\x07") == []


def test_split_head_drops_generated_lines_and_needs_the_head() -> None:
    head = ["# Mine", "Read me first."]
    warning = "- Warning: 3 resident entries did not fit"
    generated = {link_line(L1), warning}.__contains__
    lines = ["# Mine", "", "  Read  me first.", warning, link_line(L1), "", "- added", "## S", "- x"]
    assert split_head(lines, head, generated) == (["- added", "## S", "- x"], "")
    assert split_head(["# Mine", "Read me first.", "### T"], head, generated) == (["### T"], "")
    assert split_head(["# Mine", "Read me first.", "more text", "## S"], head, generated) == (None, "more text")
    assert split_head(["# Mine", "Changed.", "## S"], head, generated) == (None, "Changed.")
    assert split_head(["# Mine"], head, generated) == (None, "")
    assert split_head(["## S", "- x"], [], generated) == (["## S", "- x"], "")


def test_warning_lines_of_any_count() -> None:
    r = render([item("x" * 200, order=n, file=f"f{n}.md") for n in range(120)], None, Config(budget_bytes=4000))
    (line,) = [ln for ln in r.l0.split("\n") if ln.startswith("- Warning:")]
    assert is_warning_line(line, [L1]) and is_warning_line(line.replace(" ", "   "), ["a.md", L1])
    assert is_warning_line(line.replace(str(len(r.overflow)), "7"), [L1])
    assert not is_warning_line(line, ["other.md"]) and not is_warning_line("- Warning: 1 thing", [L1])


def test_memory_links() -> None:
    text = "[a](a.md) [web](https://x.org/b.md) [anchor](c.md#top) [mail](mailto:d.md) [up](../e.md) [f]( f.md)"
    assert memory_links(text) == ["a.md", "mailto:d.md", "../e.md"]


def _ctx(sources: list[Source], pending: frozenset[str] = frozenset(), items: Optional[list] = None) -> Context:
    rendered = render(items or [], None, Config())
    return Context(rendered, None, (L1,), {s.name: s for s in sources}, pending, 40, default_pieces(sources))


def _l0(*body: str) -> Edited:
    return Edited("MEMORY.md", "l0", "\n".join(head_lines(None, L1) + list(body)) + "\n")


def test_classification_and_orders() -> None:
    sources = [source("new.md", name="New"), source("ruled.md", idx="[R](ruled.md) — set"), source("p.md")]
    ctx = _ctx(sources, frozenset({"p.md"}))
    out = analyse([_l0("## S", "- free · [New](new.md) — added · [web](https://w.md)", "### T",
                       "- [n](new.md) again", "- [p](p.md) x", "- [r](ruled.md) y", "- [g](gone.md) z",
                       "- [a](new.md) [b](p.md)", "- [u](../u.md)", "- free")], ctx)
    assert out.entries == [PendingEntry("new.md", "[New](new.md) — added", "", "", 41)]
    assert out.lines == [ManualLine("free", "S", "", 42), ManualLine("[web](https://w.md)", "S", "", 43)]
    reasons = [(u.segment, u.reason.split(";")[0]) for u in out.unrescuable]
    assert reasons == [
        ("[n](new.md) again", "another added line already links to new.md"),
        ("[p](p.md) x", "p.md already has a line waiting in .index-pending.json"),
        ("[r](ruled.md) y", "ruled.md already has an index line (metadata.idx), so this is an edited generated line"),
        ("[g](gone.md) z", "gone.md does not exist or could not be read"),
        ("[a](new.md) [b](p.md)", "links to 2 memory files, so it cannot be one file's index line"),
        ("[u](../u.md)", "link target '../u.md' is not a memory file in this directory"),
    ]
    assert all(u.reason.endswith("edit the memory file's frontmatter (metadata.idx) instead") for u in out.unrescuable)


def test_generated_head_lines_may_be_missing_or_moved() -> None:
    ctx = _ctx([source("a.md")])
    bare = Edited("MEMORY.md", "l0", "## S\n- loose\n")
    assert analyse([bare], ctx).lines == [ManualLine("loose", "S", "", 41)]
    moved = Edited("MEMORY.md", "l0", "\n".join([link_line("old.md"), TITLE, NOTE, "- loose", "## S"]))
    assert analyse([moved], ctx).lines == [ManualLine("loose", "Other", "", 41)]
    (u,) = analyse([Edited("MEMORY.md", "l0", "# My index\n" + "\n".join(head_lines(None, L1)))], ctx).unrescuable
    assert (u.segment, u.reason) == ("# My index", "preamble edited — change it in .index-manual.json")
    mine = render([], "# Mine", Config())
    manual = Context(mine, "# Mine", ctx.l1_names, ctx.sources, ctx.pending_files, 40, ctx.defaults)
    (u,) = analyse([Edited("MEMORY.md", "l0", "\n".join(head_lines(None, L1)) + "\n- x\n")], manual).unrescuable
    assert u.segment == TITLE


def test_present_and_default_lines_are_not_added() -> None:
    sources = [source("a.md", name="A", description="about a"), source("b.md", idx="[B](b.md) — mine")]
    items = [item("[B](b.md) — mine", "S", file="b.md")]
    edited = _l0("## Elsewhere", "- [A](a.md) — about a · [B](b.md)   —  mine", "- [b](b.md)")
    out = analyse([edited], _ctx(sources, items=items))
    assert (out.entries, out.lines, out.unrescuable) == ([], [], [])
    assert out.pieces["l0"] == ["[A](a.md) — about a", "[B](b.md) — mine", "[b](b.md)"]


def test_l1_reserved_sections_and_head() -> None:
    ctx = _ctx([source("a.md")])
    l1 = Edited(L1, "l1", "\n".join(l1_head(L1) + ["## Overflow", "- [a](a.md) — moved", "- loose"]))
    out = analyse([l1], ctx)
    assert out.entries == [PendingEntry("a.md", "[a](a.md) — moved", "", "", 41)]
    assert out.lines == [ManualLine("loose", "Other", "", 42)]
    edited = Edited(L1, "l1", "\n".join(l1_head(L1)).replace(L1_NOTE, "my note") + "\n- x\n")
    (u,) = analyse([edited], ctx).unrescuable
    assert (u.segment, u.reason.split(";")[0]) == ("my note", "the generated header was edited")
    (u,) = analyse([Edited(L1, "l1", "\n".join(l1_head(L1) + ["stray text", "## S"]))], ctx).unrescuable
    assert u.segment == "stray text"
    (u,) = analyse([Edited("MEMORY.md", "l0", None)], ctx).unrescuable
    assert u.reason.startswith("not valid UTF-8")


def test_lost_counts_only_what_a_file_could_lose() -> None:
    fresh, new = {"a", "b", "gone-source"}, {"a", "b"}
    edited = (None, ["a", "hand", "default"])
    written = ("## S\n- a · b · gone-source · stale\n", None)
    assert lost([edited, written], fresh, new, {"default"}) == ["hand", "gone-source"]
    assert lost([edited, written], fresh, new | {"hand"}, {"default", "gone-source"}) == []
