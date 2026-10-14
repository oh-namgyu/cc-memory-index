from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from storekit import make_store, source, write_json

from cc_memory_index import items as items_mod
from cc_memory_index.frontmatter import parse
from cc_memory_index.items import (
    DEFAULT_ORDER,
    MANUAL_NAME,
    PENDING_NAME,
    DataFileError,
    Item,
    Manual,
    ManualLine,
    PendingEntry,
    items_from_sources,
    load_manual,
    load_pending,
    manual_items,
)
from cc_memory_index.safeio import Fingerprint, StoreDir
from cc_memory_index.store import Source


def _pending(file: str, idx: str, section: str = "", sub: str = "", order: int = 7) -> PendingEntry:
    return PendingEntry(file, idx, section, sub, order)


def test_defaults_from_frontmatter() -> None:
    srcs = [
        source("full.md", name="Full", description="all keys", type="reference", tier="resident",
               idx="[F](full.md) — custom", idx_section="Ref", idx_sub="Sub", idx_order=40),
        source("plain.md", name="Plain", description="a note", type="feedback"),
        source("bare.md"),
        source("nodesc.md", name="No desc", type="project"),
    ]
    out = items_from_sources(srcs, [])
    assert out == [
        Item("full.md", "[F](full.md) — custom", "Ref", "Sub", 40, True, "source"),
        Item("plain.md", "[Plain](plain.md) — a note", "Feedback", "", DEFAULT_ORDER, False, "source"),
        Item("bare.md", "[bare](bare.md)", "Other", "", DEFAULT_ORDER, False, "source"),
        Item("nodesc.md", "[No desc](nodesc.md)", "Project", "", DEFAULT_ORDER, False, "source"),
    ]


def test_top_level_keys_and_metadata_precedence() -> None:
    text = "---\nname: A\ntier: resident\nidx: top\nmetadata:\n  idx: meta\n---\n"
    src = Source("a.md", text, Fingerprint(0, ""), parse(text))
    (it,) = items_from_sources([src], [])
    assert it.idx == "meta" and it.resident


def test_text_is_sanitised() -> None:
    src = source("a.md", idx="line two\tthree\x00", idx_section="Sec\x7ftion", idx_sub=" sub ")
    (it,) = items_from_sources([src], [])
    assert (it.idx, it.section, it.sub) == ("line two three", "Section", "sub")


def test_tier_must_be_exactly_resident() -> None:
    out = items_from_sources([source("a.md", tier="Resident"), source("b.md", tier="l1")], [])
    assert [it.resident for it in out] == [False, False]


@pytest.mark.parametrize("raw, expected", [("12", 12), ("-3", -3), ("+5", 5), (" 9 ", 9), ("", DEFAULT_ORDER)])
def test_idx_order_integers(raw: str, expected: int) -> None:
    problems: list[str] = []
    (it,) = items_from_sources([source("a.md", idx_order=raw)], [], problems)
    assert it.order == expected and problems == []


@pytest.mark.parametrize("raw", ["1.5", "ten", "1_000", "1" * 19, "٣"])
def test_idx_order_non_integer_is_reported(raw: str) -> None:
    problems: list[str] = []
    (it,) = items_from_sources([source("a.md", idx_order=raw)], [], problems)
    assert it.order == DEFAULT_ORDER
    assert len(problems) == 1 and "a.md" in problems[0] and "idx_order" in problems[0]


def test_pending_is_used_only_without_idx() -> None:
    srcs = [source("a.md", type="user", tier="resident"), source("b.md", idx="same"), source("c.md", idx="mine")]
    pending = [_pending("a.md", "rescued a", "Sec", "Sub", 3), _pending("b.md", " same "), _pending("c.md", "theirs")]
    out = items_from_sources(srcs, pending)
    assert out == [
        Item("a.md", "rescued a", "Sec", "Sub", 3, True, "source"),
        Item("b.md", "same", "Other", "", DEFAULT_ORDER, False, "source"),
        Item("c.md", "mine", "Other", "", DEFAULT_ORDER, False, "source"),
        Item("c.md", "theirs", "Other", "", 7, False, "pending"),
    ]


def test_pending_section_falls_back_to_the_type() -> None:
    (it,) = items_from_sources([source("a.md", type="feedback")], [_pending("a.md", "x")])
    assert it.section == "Feedback"


def test_second_pending_entry_and_orphans_are_unresolved() -> None:
    problems: list[str] = []
    pending = [_pending("a.md", "first"), _pending("a.md", "second"), _pending("gone.md", "orphan"),
               _pending("a.md", "first")]
    out = items_from_sources([source("a.md")], pending, problems)
    assert [(it.idx, it.origin) for it in out] == [("first", "source"), ("second", "pending"), ("orphan", "pending")]
    assert len(problems) == 1 and "gone.md" in problems[0]


def test_manual_items_are_resident() -> None:
    manual = Manual(None, [ManualLine("free\ttext", "", "", 5), ManualLine("x", "Sec", "Sub", -1)])
    assert manual_items(manual) == [
        Item(None, "free text", "Other", "", 5, True, "manual"),
        Item(None, "x", "Sec", "Sub", -1, True, "manual"),
    ]


def _sd(tmp_path: Path) -> StoreDir:
    return StoreDir(str(make_store(tmp_path)))


def test_absent_data_files(tmp_path: Path) -> None:
    with _sd(tmp_path) as sd:
        assert load_manual(sd) == Manual(None, [])
        assert load_pending(sd) == []


def test_load_valid_data_files(tmp_path: Path) -> None:
    with _sd(tmp_path) as sd:
        root = Path(sd.path)
        write_json(root, MANUAL_NAME, {"version": 1, "preamble": "# Hi\n", "lines": [
            {"seg": "a", "section": "S", "sub": "", "order": 1}]})
        write_json(root, PENDING_NAME, {"version": 1, "entries": [
            {"file": "a.md", "idx": "[a](a.md)", "section": "S", "sub": "T", "order": 2}]})
        assert load_manual(sd) == Manual("# Hi\n", [ManualLine("a", "S", "", 1)])
        assert load_pending(sd) == [PendingEntry("a.md", "[a](a.md)", "S", "T", 2)]


def _line(**override: Any) -> dict[str, Any]:
    return {"seg": "a", "section": "S", "sub": "", "order": 1, **override}


def _entry(**override: Any) -> dict[str, Any]:
    return {"file": "a.md", "idx": "x", "section": "S", "sub": "", "order": 1, **override}


@pytest.mark.parametrize("text", [
    '{"version": 1, "preamble": null, "lines": [], "lines": []}',
    "[]",
    "{not json",
    pytest.param("[" * 100000, id="deeply-nested"),
    json.dumps({"version": 2, "preamble": None, "lines": []}),
    json.dumps({"version": True, "preamble": None, "lines": []}),
    json.dumps({"version": 1, "preamble": None}),
    json.dumps({"version": 1, "preamble": None, "lines": [], "extra": 1}),
    json.dumps({"version": 1, "preamble": 3, "lines": []}),
    json.dumps({"version": 1, "preamble": "\ud800", "lines": []}),
    json.dumps({"version": 1, "preamble": None, "lines": {}}),
    json.dumps({"version": 1, "preamble": None, "lines": [_line(order=True)]}),
    json.dumps({"version": 1, "preamble": None, "lines": [_line(order=1.0)]}),
    json.dumps({"version": 1, "preamble": None, "lines": [_line(order=10**18)]}),
    json.dumps({"version": 1, "preamble": None, "lines": [_line(seg=" \t")]}),
    json.dumps({"version": 1, "preamble": None, "lines": [_line(section=None)]}),
    json.dumps({"version": 1, "preamble": None, "lines": [{"seg": "a"}]}),
    json.dumps({"version": 1, "preamble": None, "lines": ["a"]}),
])
def test_manual_file_is_strict(tmp_path: Path, text: str) -> None:
    with _sd(tmp_path) as sd:
        (Path(sd.path) / MANUAL_NAME).write_text(text, encoding="utf-8")
        with pytest.raises(DataFileError):
            load_manual(sd)


@pytest.mark.parametrize("entry", [
    _entry(file="../a.md"), _entry(file="a.txt"), _entry(file=".a.md"), _entry(idx=""), _entry(idx=5),
    _entry(order="1"), _entry(sub=None), {"file": "a.md", "idx": "x"},
])
def test_pending_file_is_strict(tmp_path: Path, entry: dict[str, Any]) -> None:
    with _sd(tmp_path) as sd:
        write_json(Path(sd.path), PENDING_NAME, {"version": 1, "entries": [entry]})
        with pytest.raises(DataFileError):
            load_pending(sd)


def test_data_file_size_cap_and_links(tmp_path: Path, symlink: Any) -> None:
    with _sd(tmp_path) as sd:
        root = Path(sd.path)
        (root / PENDING_NAME).write_bytes(b" " * (items_mod.DATA_MAX_BYTES + 1))
        with pytest.raises(DataFileError, match="cannot read"):
            load_pending(sd)
        outside = tmp_path / "outside.json"
        outside.write_text('{"version": 1, "preamble": null, "lines": []}')
        symlink(root / MANUAL_NAME, outside)
        with pytest.raises(DataFileError, match="cannot read"):
            load_manual(sd)
