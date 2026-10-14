from __future__ import annotations

import pytest

from cc_memory_index.text import normalize_ws, sanitize, split_segments


def test_sanitize_turns_line_breaks_and_tabs_into_spaces() -> None:
    assert sanitize("a\r\nb\tc\nd\re") == "a  b c d e"


def test_sanitize_removes_other_controls_nul_and_del() -> None:
    assert sanitize("\x00a\x01b\x08\x0b\x0c\x0e\x1f\x7fc") == "abc"


def test_sanitize_strips_and_keeps_ordinary_unicode() -> None:
    assert sanitize("  \t작업 — [x](x.md) · y \n") == "작업 — [x](x.md) · y"


def test_sanitize_treats_unicode_line_separators_as_breaks() -> None:
    assert sanitize("a b c\u0085d") == "a b c d"


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
