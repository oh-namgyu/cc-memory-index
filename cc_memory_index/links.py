"""Markdown links to memory files in index text: the one extractor used by rescue, migrate and lint
(DESIGN §5.2, §6, §8).

A link is `[text](target)` on one line: the text holds no brackets, and the target is non-empty,
holds no whitespace, brackets, parentheses or angle brackets, and may be wrapped in `<` `>`. A
`#fragment` is not part of the file name, so `[N](n.md#top)` links to n.md. A target with a URL
scheme (`https:`, `mailto:` ...) is never a memory file, and nothing inside an inline code span is a
link (markdown.blank_code). Neither the text nor the target can contain `[`, so every character is
scanned by at most one attempt and the match time is linear, and each line is read up to
LINE_MAX_CHARS only.
"""

from __future__ import annotations

import re

from .markdown import blank_code

LINE_MAX_CHARS = 64 * 1024
_LINK = re.compile(r"\[[^\[\]\n]*\]\((?:<([^<>()\[\]\s]+)>|([^<>()\[\]\s]+))\)")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_SUFFIX = ".md"


def _target(raw: str) -> str:
    """The file a link target names: everything before its fragment."""
    return raw.split("#", 1)[0]


def memory_links(text: str) -> list[str]:
    """The targets of the links in text that name a .md file, in order and with repeats.

    Targets are returned as written (fragment removed), so a caller still has to check that one
    is a valid source name before using it.
    """
    found: list[str] = []
    for line in text.split("\n"):
        for m in _LINK.finditer(blank_code(line[:LINE_MAX_CHARS])):
            target = _target(m.group(1) or m.group(2))
            if target.endswith(_SUFFIX) and not _SCHEME.match(target):
                found.append(target)
    return found
