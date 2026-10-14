from __future__ import annotations

import dataclasses
import random

import pytest
from storekit import item

from cc_memory_index.items import Item, PendingEntry
from cc_memory_index.render import (
    BULLET,
    L1_DESCRIPTION,
    L1_NOTE,
    L1_TITLE,
    NOTE,
    TITLE,
    BudgetError,
    Rendered,
    fill,
    head_lines,
    link_line,
    nbytes,
    packable,
    reachable,
    render,
    render_order,
    size_of,
    warning_line,
)
from cc_memory_index.store import Config
from cc_memory_index.text import sanitize

L1 = "index-l1.md"
BIG = Config(budget_bytes=25000, budget_lines=200)


def _l0_body(rendered: Rendered) -> list[str]:
    lines = rendered.l0.split("\n")
    return lines[lines.index(link_line(L1)) + 1:-1]


def test_default_l0_layout_is_exact() -> None:
    items = [item("a1", "A", order=2, file="a1.md"), item("a2", "A", "Sub", order=3, file="a2.md"),
             item("b1", "B", order=1, file="b1.md"), item("off", "B", resident=False, file="off.md")]
    r = render(items, None, Config())
    assert r.l0 == "\n".join([
        TITLE, "", NOTE, "", link_line(L1),
        "", "## B", "", "- b1",
        "", "## A", "", "- a1", "", "### Sub", "", "- a2",
    ]) + "\n"
    assert len([TITLE, "", NOTE]) <= 3 and "\r" not in r.l0 + r.l1
    assert (r.overflow, r.oversized, r.unresolved) == ([], [], [])


def test_l1_layout_is_exact() -> None:
    items = [item("x", "X", resident=False, file="x.md"), item("y", "X", "Deep", resident=False, file="y.md"),
             item("p", "P", file="p.md", origin="pending", resident=False)]
    r = render(items, None, Config())
    assert r.l1 == "\n".join([
        "---", "name: index-l1", f"description: {L1_DESCRIPTION}", "metadata:", "  type: reference",
        "  generated: true", "---", "", L1_TITLE, "", L1_NOTE,
        "", "## X", "", "- x", "", "### Deep", "", "- y",
        "", "## Unresolved", "", "- p",
    ]) + "\n"
    assert [it.idx for it in r.unresolved] == ["p"]


def test_l1_is_always_produced_and_named_after_the_config() -> None:
    r = render([], None, Config(l1_name="more.md"))
    assert r.l1.startswith("---\nname: more\n") and r.l1.endswith(L1_NOTE + "\n")
    assert "[more.md](more.md)" in r.l0 and "## " not in r.l1


def test_ordering_sections_subsections_items() -> None:
    a1 = item("a1", "A", "", 50, "a1.md")
    a2 = item("a2", "A", "Y", 1, "a2.md")
    a3 = item("a3", "A", "X", 1, "z.md")
    a4 = item("a4", "A", "X", 1, "a.md")
    a5 = item("a5", "A", "X", 0, "m.md")
    b1, c1 = item("b1", "B", "", 10, "b.md"), item("c1", "C", "", 10, "c.md")
    manual = item("m", "B", "", 10, origin="manual")
    order = render_order([c1, b1, a2, a1, a3, a4, a5, manual])
    assert order == [a1, a5, a4, a3, a2, manual, b1, c1]


def test_render_is_deterministic_and_input_order_independent() -> None:
    items = [item(f"i{n}", f"S{n % 3}", f"sub{n % 2}" if n % 4 else "", n % 5, f"f{n}.md") for n in range(40)]
    first = render(items, None, Config())
    assert render(list(reversed(items)), None, Config()) == first == render(items, None, Config())


def _packed(idx_list: list[str], line_max: int = 80) -> list[str]:
    items = [item(idx, order=n, file=f"f{n:02d}.md") for n, idx in enumerate(idx_list)]
    body = _l0_body(render(items, None, Config(line_max_bytes=line_max)))
    return [line for line in body if line.startswith(BULLET)]


def test_packing_joins_up_to_the_line_cap() -> None:
    a, b, c = "a" * 30, "b" * 30, "c" * 30
    assert _packed([a, b, c]) == [f"- {a} · {b}", f"- {c}"]
    assert nbytes(f"- {a} · {b}") == 66


def test_packing_exactly_at_the_line_cap() -> None:
    a, b = "a" * 37, "b" * 37
    assert _packed([a, b]) == [f"- {a} · {b}"] and nbytes(f"- {a} · {b}") == 80
    assert _packed([a, b + "b"]) == [f"- {a}", f"- {b}b"]


def test_item_longer_than_the_line_cap_sits_alone() -> None:
    short1, long, short2, short3 = "s" * 10, "L" * 100, "t" * 10, "u" * 10
    assert _packed([short1, long, short2, short3]) == [f"- {short1}", f"- {long}", f"- {short2} · {short3}"]


def test_unpackable_items_sit_alone() -> None:
    assert not packable("[open") and not packable("a · b") and packable("[ok](ok.md) — x")
    assert _packed(["s1", "[open", "s2", "a · b", "s3", "s4"]) == ["- s1", "- [open", "- s2", "- a · b", "- s3 · s4"]


def _sections(n: int, size: int = 150) -> list[Item]:
    return [item(f"{n2:03d}" + "x" * (size - 3), f"S{n2:02d}", order=n2, file=f"f{n2:02d}.md") for n2 in range(n)]


def test_budget_bytes_boundary() -> None:
    items = _sections(14)
    full = render(items, None, BIG).l0
    exact = nbytes(full) + nbytes(warning_line(14, L1)) + 1
    assert 2048 <= exact <= 25000
    fits = render(items, None, Config(budget_bytes=exact, budget_lines=200))
    assert fits.l0 == full and fits.overflow == [] and "Warning" not in fits.l0
    over = render(items, None, Config(budget_bytes=exact - 1, budget_lines=200))
    assert over.overflow == [items[-1]]
    assert warning_line(1, L1) in over.l0 and nbytes(over.l0) <= exact - 1
    assert BULLET + items[-1].idx in over.l1.split("## Overflow")[1]


def test_budget_lines_boundary() -> None:
    items = _sections(6, 10)
    full = render(items, None, BIG).l0
    exact = full.count("\n") + 1
    assert exact >= 20
    fits = render(items, None, Config(budget_bytes=25000, budget_lines=exact))
    assert fits.l0 == full and fits.overflow == []
    over = render(items, None, Config(budget_bytes=25000, budget_lines=exact - 1))
    assert over.overflow == [items[-1]] and over.l0.count("\n") <= exact - 1


def test_fixed_cost_rejection() -> None:
    with pytest.raises(BudgetError, match="fixed part"):
        render([], None, Config(budget_bytes=2048, item_max_bytes=2000))
    with pytest.raises(BudgetError):
        render([], "\n".join(f"line {n}" for n in range(140)), Config())
    with pytest.raises(BudgetError):
        render([], "p" * 18500, Config())
    render([], "\n".join(f"line {n}" for n in range(120)), Config())


def test_oversized_item_goes_to_overflow_untruncated() -> None:
    huge = "y" * 81
    items = [item("small", order=1, file="s.md"), item(huge, order=2, file="h.md"), item("tail", order=3, file="t.md")]
    r = render(items, None, Config(item_max_bytes=80))
    assert [it.idx for it in r.oversized] == [huge] and [it.idx for it in r.overflow] == [huge]
    assert huge not in r.l0 and "- small · tail" in r.l0 and warning_line(1, L1) in r.l0
    assert r.l1.endswith("## Overflow\n\n- " + huge + "\n")


def test_overflow_keeps_render_order_and_includes_manual_lines() -> None:
    items = _sections(30)
    items.insert(5, item("manual " + "m" * 140, "S20", order=20, origin="manual"))
    r = render(items, None, Config(budget_bytes=3000))
    order = render_order(items)
    admitted = len(order) - len(r.overflow)
    assert r.overflow == order[admitted:] and any(it.origin == "manual" for it in r.overflow)
    listed = r.l1.split("## Overflow\n\n")[1].rstrip("\n").split("\n")
    assert listed == [BULLET + it.idx for it in r.overflow]
    assert warning_line(len(r.overflow), L1) in r.l0 and nbytes(r.l0) <= 3000


def test_manual_preamble_replaces_the_default() -> None:
    r = render([], "\n\n  # My index\r\n\tindented\x07 line next  \n\n", Config())
    assert r.l0.startswith("  # My index\n indented line\nnext\n\n" + link_line(L1))
    assert head_lines("  \n\t", L1)[:3] == [TITLE, "", NOTE]


def _random_items(rng: random.Random) -> list[Item]:
    alphabet = "abcdeé가 []()·-—"
    out: list[Item] = []
    for n in range(rng.randrange(0, rng.choice([10, 40, 90]))):
        length = rng.randrange(40, 500) if rng.random() < 0.05 else rng.randrange(1, 40)
        idx = sanitize("".join(rng.choice(alphabet) for _ in range(length))) or "z"
        if rng.random() < 0.2:
            idx = idx + " · " + idx
        origin = rng.choice(["source"] * 6 + ["manual", "pending"])
        out.append(Item(None if origin == "manual" else f"f{n}.md", idx, rng.choice("ABCD"), rng.choice(["", "x", "y"]),
                        rng.randrange(-3, 12), rng.random() < 0.7 or origin == "manual", origin))
    return out


def _random_config(rng: random.Random) -> Config:
    return Config(budget_bytes=rng.randrange(2048, 7000), budget_lines=rng.randrange(20, 70),
                  item_max_bytes=rng.randrange(80, 400), line_max_bytes=rng.randrange(80, 300))


def test_property_budget_warning_reachability_and_incremental_size() -> None:
    rng = random.Random(20261014)
    checked = 0
    for _ in range(400):
        items, cfg = _random_items(rng), _random_config(rng)
        try:
            r = render(items, None, cfg)
        except BudgetError:
            continue
        checked += 1
        assert nbytes(r.l0) <= cfg.budget_bytes and r.l0.count("\n") <= cfg.budget_lines
        has_warning = any(line.startswith("- Warning:") for line in r.l0.split("\n"))
        assert has_warning == bool(r.overflow)
        resident = [it for it in items if it.resident and it.origin != "pending"]
        order = render_order(resident)
        assert r.overflow == [it for it in order if it in r.overflow]
        rest = [it for it in order if nbytes(it.idx) <= cfg.item_max_bytes]
        spilled = [it for it in r.overflow if nbytes(it.idx) <= cfg.item_max_bytes]
        assert rest[len(rest) - len(spilled):] == spilled
        assert reachable(items, r) == []
        head = head_lines(None, cfg.l1_name)
        filled = fill(head, resident, cfg)
        warning = [warning_line(len(r.overflow), cfg.l1_name)] if r.overflow else []
        assert size_of(head + warning) + filled.body.size == nbytes(r.l0)
        assert len(head) + len(warning) + len(filled.body.lines) == r.l0.count("\n")
        assert render(items, None, cfg) == r
    assert checked > 300


def test_reachable_reports_missing_items_and_pending() -> None:
    items = [item("same", file="a.md"), item("same", file="b.md"), item("m", origin="manual"),
             item("u", file="c.md", origin="pending", resident=False)]
    r = render(items, None, Config())
    assert reachable(items, r) == []
    assert reachable(items, dataclasses.replace(r, l0=r.l0.replace("- m · same · same", "- m · same"))) == ["b.md"]
    assert reachable(items, dataclasses.replace(r, l1=r.l1.replace("- u\n", ""))) == ["pending line for c.md"]
    pending = [PendingEntry("a.md", "same", "", "", 0), PendingEntry("z.md", "nowhere", "", "", 0)]
    assert reachable(items, r, pending) == ["pending line for z.md"]


def test_l1_sections_named_like_its_own_headings_get_a_suffix() -> None:
    items = [item("o", "Overflow", resident=False, file="o.md"), item("u", "unresolved", resident=False, file="u.md"),
             item("p", "P", file="p.md", origin="pending", resident=False), item("r", "Overflow", file="r.md")]
    r = render(items, None, Config())
    assert r.l1.split(L1_NOTE + "\n")[1] == "\n".join([
        "", "## Overflow (section)", "", "- o", "", "## unresolved (section)", "", "- u",
        "", "## Unresolved", "", "- p",
    ]) + "\n"
    assert "## Overflow\n\n- r\n" in r.l0
