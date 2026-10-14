from __future__ import annotations

import pytest

from cc_memory_index.frontmatter import WINDOW_BYTES, PatchError, parse, patch
from cc_memory_index.text import sanitize

DOC = (
    "---\n"
    "name: build-commands\n"
    "description: How to build\n"
    "type: top-level-type\n"
    "metadata:\n"
    "  type: reference\n"
    "  tier: resident   # resident = L0\n"
    '  idx: "[Build](build.md) — make test"\n'
    "---\n"
    "Body text\n"
)

NESTED = (
    "---\n"
    "name: n\n"
    "tags:\n"
    "  - a\n"
    "  - b\n"
    "flow: [1, 2]\n"
    "block: |\n"
    "  line one\n"
    "  line two\n"
    "plain: first\n"
    "  continued\n"
    "metadata:\n"
    "  type: project\n"
    "  deep:\n"
    "    inner: 1\n"
    "  list:\n"
    "    - q\n"
    "# trailing comment\n"
    "---\n"
    "body\n"
)


def _body(text: str) -> str:
    return text[text.index("\n---\n") + 5:]


def test_metadata_first_then_top_level() -> None:
    fm = parse(DOC)
    assert (fm.present, fm.ok, fm.error) == (True, True, None)
    assert fm.get("type") == "reference"
    assert fm.top["type"] == "top-level-type"
    assert fm.get("name") == "build-commands"
    assert fm.get("tier") == "resident"
    assert fm.get("idx") == "[Build](build.md) — make test"
    assert fm.get("missing") is None


def test_quoted_values_and_escapes() -> None:
    text = (
        "---\n"
        'a: "say \\"hi\\" \\\\ back"\n'
        "b: 'it''s'\n"
        'c: "x" # note\n'
        'd: "unterminated\n'
        'e: "x" trailing\n'
        "f: ''\n"
        'g: "keep \\n as text"\n'
        "h: bare # comment\n"
        "i: a#b\n"
        "j:\n"
        "k: # only a comment\n"
        "---\n"
    )
    assert parse(text).top == {
        "a": 'say "hi" \\ back',
        "b": "it's",
        "c": "x",
        "f": "",
        "g": "keep \\n as text",
        "h": "bare",
        "i": "a#b",
    }


def test_bom_and_crlf() -> None:
    assert parse("\ufeff---\nname: x\n---\n").get("name") == "x"
    fm = parse(DOC.replace("\n", "\r\n"))
    assert fm.ok and fm.get("tier") == "resident" and fm.get("name") == "build-commands"
    assert fm.get("idx") == "[Build](build.md) — make test"


def test_text_without_frontmatter() -> None:
    for text in ("# Title\n---\n", "", " ---\nname: x\n---\n", "---x\n---\n"):
        fm = parse(text)
        assert (fm.present, fm.ok, fm.top, fm.metadata) == (False, True, {}, {})


def test_missing_closing_line_is_unparseable() -> None:
    fm = parse("---\nname: x\n")
    assert fm.present and not fm.ok and fm.error


def test_closing_line_must_be_within_64_kib() -> None:
    inside = "---\nname: x\n" + "# f\n" * 1000 + "---\n"
    assert parse(inside).ok
    outside = "---\nname: x\n" + "# filler\n" * (WINDOW_BYTES // 9 + 10) + "---\n"
    fm = parse(outside)
    assert fm.present and not fm.ok and fm.top == {}


def test_window_is_measured_in_utf8_bytes() -> None:
    assert parse("---\n# " + "가" * 20000 + "\n---\n").ok
    assert not parse("---\n# " + "가" * 30000 + "\n---\n").ok


def test_lists_nesting_and_multiline_scalars_are_ignored() -> None:
    fm = parse(NESTED)
    assert fm.top == {"name": "n"}
    assert fm.metadata == {"type": "project"}


def test_patch_preserves_unknown_lines_byte_for_byte() -> None:
    out = patch(NESTED, {"idx": "line"})
    added = '  idx: "line"\n'
    assert out.replace(added, "", 1) == NESTED
    assert out.index(added) < out.index("# trailing comment")
    assert parse(out).metadata == {"type": "project", "idx": "line"}


def test_patch_adds_only_missing_keys() -> None:
    out = patch(
        DOC,
        {"tier": "l1", "idx": "other", "name": "zzz", "idx_section": "Reference", "idx_order": 40},
    )
    fm = parse(out)
    assert fm.get("tier") == "resident"
    assert fm.get("idx") == "[Build](build.md) — make test"
    assert fm.get("name") == "build-commands" and "name" not in fm.metadata
    assert fm.metadata["idx_section"] == "Reference"
    assert '  idx_section: "Reference"\n  idx_order: 40\n---\n' in out
    assert out.replace('  idx_section: "Reference"\n  idx_order: 40\n', "") == DOC


def test_patch_is_idempotent_and_noop_keeps_text() -> None:
    once = patch(DOC, {"idx_sub": "s", "idx_order": 1})
    assert patch(once, {"idx_sub": "s", "idx_order": 1}) == once
    assert patch(DOC, {"tier": "l1", "name": "x"}) == DOC
    assert patch(DOC, {}) == DOC


def test_patch_keeps_crlf() -> None:
    crlf = DOC.replace("\n", "\r\n")
    out = patch(crlf, {"idx_sub": "s"})
    assert '  idx_sub: "s"\r\n' in out
    assert "\n" not in out.replace("\r\n", "")


def test_patch_creates_metadata_block_at_end_of_frontmatter() -> None:
    out = patch("---\nname: a\ndescription: d\n---\nbody\n", {"idx": "x"})
    assert out == '---\nname: a\ndescription: d\nmetadata:\n  idx: "x"\n---\nbody\n'


def test_patch_uses_existing_indent_and_fills_empty_block() -> None:
    four = "---\nmetadata:\n    type: user\n---\n"
    assert patch(four, {"idx": "x"}) == '---\nmetadata:\n    type: user\n    idx: "x"\n---\n'
    empty = "---\nname: a\nmetadata:\nother: 1\n---\n"
    assert patch(empty, {"idx": "x"}) == '---\nname: a\nmetadata:\n  idx: "x"\nother: 1\n---\n'


def test_patch_keeps_bom_and_closing_line_at_eof() -> None:
    out = patch("\ufeff---\nname: a\n---", {"idx": "x"})
    assert out == '\ufeff---\nname: a\nmetadata:\n  idx: "x"\n---'


def test_patch_injection_cannot_add_lines_close_frontmatter_or_add_keys() -> None:
    evil = 'x"\n---\nevil: 1\n'
    before = parse(DOC)
    out = patch(DOC, {"idx_sub": evil})
    after = parse(out)
    assert set(after.top) == set(before.top)
    assert set(after.metadata) == set(before.metadata) | {"idx_sub"}
    assert after.get("idx_sub") == 'x" --- evil: 1'
    assert out.count("\n") == DOC.count("\n") + 1
    assert _body(out) == _body(DOC) == "Body text\n"


@pytest.mark.parametrize(
    "value",
    [
        'x"\n---\nevil: 1\n',
        "\\",
        '\\"',
        '"',
        "ends with backslash \\",
        "# not a comment",
        "a # b",
        "'single'",
        "\u2028---\u2028evil: 1",
        "tab\there\x00nul\x7f",
        "[link](x.md) · y: z",
    ],
)
def test_patch_values_round_trip(value: str) -> None:
    out = patch(DOC, {"idx_sub": value})
    after = parse(out)
    assert after.get("idx_sub") == sanitize(value)
    assert set(after.metadata) == set(parse(DOC).metadata) | {"idx_sub"}
    assert _body(out) == "Body text\n"


@pytest.mark.parametrize("key", ["a: b", "a\nb", "metadata", "", " x", "-x"])
def test_patch_rejects_bad_keys(key: str) -> None:
    with pytest.raises(ValueError):
        patch(DOC, {key: "x"})


@pytest.mark.parametrize("value", [1.5, True, None, b"x"])
def test_patch_rejects_bad_value_types(value: object) -> None:
    with pytest.raises(TypeError):
        patch(DOC, {"idx_order": value})  # type: ignore[dict-item]


def test_replace_tier() -> None:
    out = patch(DOC, {}, {"tier": "l1"})
    assert parse(out).get("tier") == "l1"
    assert out.replace('  tier: "l1"\n', "  tier: resident   # resident = L0\n") == DOC
    assert patch(out, {}, {"tier": "l1"}) == out


def test_replace_tier_adds_it_when_missing_and_handles_top_level() -> None:
    assert patch("---\nname: a\n---\n", {}, {"tier": "l1"}) == '---\nname: a\nmetadata:\n  tier: "l1"\n---\n'
    assert patch("---\ntier: resident\n---\n", {}, {"tier": "l1"}) == '---\ntier: "l1"\n---\n'


def test_replace_tier_replaces_a_multiline_value_entirely() -> None:
    text = "---\nmetadata:\n  tier: |\n    resident\n  type: user\n---\n"
    out = patch(text, {}, {"tier": "l1"})
    assert out == '---\nmetadata:\n  tier: "l1"\n  type: user\n---\n'


def test_replace_other_key_is_refused() -> None:
    with pytest.raises(ValueError):
        patch(DOC, {}, {"idx": "x"})


def test_file_without_frontmatter_gets_a_block() -> None:
    out = patch("Just a body\n", {"idx": "x", "idx_order": 3})
    assert out == '---\nmetadata:\n  idx: "x"\n  idx_order: 3\n---\nJust a body\n'
    assert parse(out).metadata == {"idx": "x", "idx_order": "3"}
    assert patch("a\r\nb\r\n", {"idx": "x"}) == '---\r\nmetadata:\r\n  idx: "x"\r\n---\r\na\r\nb\r\n'
    bom = patch("\ufeffbody", {"idx": "x"})
    assert bom.startswith("\ufeff---\n") and parse(bom).get("idx") == "x"
    assert parse(patch("", {"idx": "x"})).get("idx") == "x"


@pytest.mark.parametrize(
    "text",
    [
        "---\nname: x\n",
        "---\nmetadata: {a: 1}\n---\n",
        "---\nmetadata:\n  - a\n---\n",
        "---\nmetadata:\n  a: 1\nmetadata:\n  b: 2\n---\n",
        "---\nmetadata:\n    a: 1\n  b: 2\n---\n",
    ],
)
def test_unpatchable_frontmatter_raises(text: str) -> None:
    with pytest.raises(PatchError):
        patch(text, {"idx": "y"})
