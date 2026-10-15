from __future__ import annotations

import os
import unicodedata

import pytest

from cc_memory_index.text import normalize_ws, sanitize, split_segments, strip_bom, utf8_prefix

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_sanitize_turns_line_breaks_and_tabs_into_spaces() -> None:
    assert sanitize("a\r\nb\tc\nd\re") == "a  b c d e"


def test_sanitize_removes_other_controls_nul_and_del() -> None:
    assert sanitize("\x00a\x01b\x08\x0b\x0c\x0e\x1f\x7fc") == "abc"


def test_sanitize_strips_and_keeps_ordinary_unicode() -> None:
    assert sanitize("  \t작업 — [x](x.md) · y \n") == "작업 — [x](x.md) · y"


def test_sanitize_treats_unicode_line_separators_as_breaks() -> None:
    assert sanitize("a\u2028b\u2029c\u0085d") == "a b c d"


def test_sanitize_result_is_one_line() -> None:
    out = sanitize("x\n---\nevil: 1\r\n")
    assert "\n" not in out and "\r" not in out
    assert out == "x --- evil: 1"


def test_normalize_ws() -> None:
    assert normalize_ws("  a \t b\n\nc  ") == "a b c"
    assert normalize_ws("") == ""


@pytest.mark.parametrize(
    "line, expected",
    [
        ("[a](a.md) — x · [b](b.md) — y", ["[a](a.md) — x", "[b](b.md) — y"]),
        ("[a · b](a.md) · c", ["[a · b](a.md)", "c"]),
        ("(x · y) · z", ["(x · y)", "z"]),
        ("[outer (inner · i) · o](o.md) · tail", ["[outer (inner · i) · o](o.md)", "tail"]),
        ("a) · b · c", ["a)", "b", "c"]),
        ("a ) ] ) · b", ["a ) ] )", "b"]),
        ("x ] · (y · z", ["x ]", "(y · z"]),
        (" · a ·  · b · ", ["a", "b"]),
        ("a·b", ["a·b"]),
        ("", []),
    ],
)
def test_split_segments(line: str, expected: list[str]) -> None:
    assert split_segments(line) == expected


def test_strip_bom_removes_exactly_one() -> None:
    assert strip_bom("\ufeff\ufeffx") == "\ufeffx" and strip_bom("x\ufeff") == "x\ufeff" and strip_bom("") == ""


def test_sanitize_removes_c1_controls_bidi_controls_and_bom() -> None:
    c1 = "".join(chr(c) for c in range(0x80, 0xA0) if c != 0x85)
    bidi = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    assert sanitize("a" + c1 + "b" + bidi + "c\ufeffd") == "abcd"
    assert sanitize("\u200b\u00a0x") == "\u200b\u00a0x".strip()


def test_utf8_prefix_never_splits_a_character() -> None:
    assert utf8_prefix("가나", 4) == "가" and utf8_prefix("ab", 2) == "ab" and utf8_prefix("ab", 1) == "a"


def _invisible(ch: str) -> bool:
    category = unicodedata.category(ch)
    return (category in ("Cc", "Cf", "Zl", "Zp") and ch not in "\n") or (category == "Zs" and ch != " ")


@pytest.mark.parametrize("folder", ["cc_memory_index", "tests"])
def test_sources_spell_invisible_characters_as_escapes(folder: str) -> None:
    found = []
    for name in sorted(os.listdir(os.path.join(ROOT, folder))):
        if name.endswith(".py"):
            with open(os.path.join(ROOT, folder, name), encoding="utf-8") as f:
                text = f.read()
            found += [f"{name}: U+{ord(ch):04X}" for ch in sorted(set(text)) if _invisible(ch)]
    assert found == []
