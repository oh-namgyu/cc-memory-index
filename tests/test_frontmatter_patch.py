"""The patcher refuses frontmatter whose structure it cannot be sure of (DESIGN §3.2)."""

from __future__ import annotations

import pytest
from test_frontmatter import DOC

from cc_memory_index.frontmatter import WINDOW_BYTES, PatchError, parse, patch, patch_checked


ADD = {"idx": "[T](t.md) — added", "idx_order": 7}


@pytest.mark.parametrize("text", [
    '---\nname: t\n"metadata":\n  type: feedback\n---\nbody\n',
    "---\nname: t\n'metadata':\n  type: feedback\n---\nbody\n",
    "---\nname: t\nmetadata :\n  type: feedback\n---\nbody\n",
    "---\n? metadata\n: {type: x}\n---\nbody\n",
    "---\nname: t\n...\nmetadata:\n  type: x\n---\nbody\n",
    "---\nname: t\n\ttype: x\n---\nbody\n",
    "---\nname: x\n- a\n---\nbody\n",
    "---\nmetadata:\n- a\n---\nbody\n",
    '---\ndescription: "multi\n  line\nmetadata:\n  type: x"\n---\nbody\n',
    "---\nmetadata:\n  note: 'open\n  tier: resident\n---\nbody\n",
    '---\ntags:\n- "open\nmetadata:\n  type: x\n---\nbody\n',
    '---\nname: t\nd: "ends with a backslash \\\n---\nbody\n',
    "---\nname: t\nmetadata:\n  type: x\u2028tier: resident\n---\nbody\n",
    "---\nname: t\x85metadata: {}\nmetadata:\n  type: x\n---\nbody\n",
])
def test_patch_refuses_structure_it_does_not_understand(text: str) -> None:
    """Audit PoC 3: these made patch add a second 'metadata' key or block, which YAML rejects."""
    for add, replace in ((ADD, None), ({}, {"tier": "indexed"})):
        with pytest.raises(PatchError, match="line [0-9]+ "):
            patch(text, add, replace)


def test_a_block_sequence_at_the_key_indentation_is_understood() -> None:
    text = "---\ntags:\n- a\n- b: c\n  - d\nmetadata:\n  type: x\n---\nbody\n"
    out = patch(text, ADD)
    assert out == text.replace("  type: x\n", '  type: x\n  idx: "[T](t.md) — added"\n  idx_order: 7\n')
    assert parse(out).metadata == {"type": "x", "idx": "[T](t.md) — added", "idx_order": "7"}


@pytest.mark.parametrize("text", [
    "--- \nname: x\n---\nbody\n",
    "\ufeff---\t\nname: x\n---\nbody\n",
    "---\nname: x\n--- \nmetadata:\n  tier: resident\n---\nbody\n",
    "---\nname: x\n---  \r\nbody\n",
])
def test_a_fence_with_trailing_whitespace_is_refused_by_parse_and_patch(text: str) -> None:
    """'--- ' is a fence for some readers and not for others: both parse and patch refuse it."""
    fm = parse(text)
    assert (fm.present, fm.ok, fm.top, fm.metadata) == (True, False, {}, {})
    assert fm.error is not None and "spaces or tabs" in fm.error
    with pytest.raises(PatchError, match="spaces or tabs"):
        patch(text, ADD)


def test_a_fence_further_down_or_indented_is_not_ambiguous() -> None:
    assert parse("---\nname: x\n---\nbody\n--- \n").get("name") == "x"
    assert parse("---\nblock: |\n  --- \nname: x\n---\n").get("name") == "x"
    assert not parse("body\n--- \n").present


def test_patch_checked_parses_the_result_again() -> None:
    assert patch_checked(DOC, {}, {"tier": "l1"}, check=("tier",)) == patch(DOC, {}, {"tier": "l1"})
    with pytest.raises(PatchError, match="'idx' key already holds a different"):
        patch_checked(DOC, {"idx": "another line"}, check=("idx",))
    edge = "---\nname: big\ndescription: " + "x" * (WINDOW_BYTES - 40) + "\n---\nbody\n"
    assert parse(edge).ok and not parse(patch(edge, {}, {"tier": "indexed"})).ok
    with pytest.raises(PatchError, match="would not parse"):
        patch_checked(edge, {}, {"tier": "indexed"}, check=("tier",))
