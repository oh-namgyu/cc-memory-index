from __future__ import annotations

from pathlib import Path

from storekit import built_store, write_json, write_memory

from cc_memory_index.build import run_build
from cc_memory_index.result import EXIT_USAGE


def test_l1_name_cannot_point_at_a_memory(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    victim = write_memory(root, "victim.md", description="a memory the config must not claim")
    original = victim.read_bytes()
    write_json(root, ".memory-index.json", {"l1_name": "victim.md"})
    for force in (False, True):
        res = run_build(str(root), force)
        assert res.code == EXIT_USAGE and "did not generate" in res.messages[-1], res.messages
        assert victim.read_bytes() == original
    assert not list(root.glob(".victim.md.bak-*"))


def test_l1_name_may_name_a_new_file_or_its_own_earlier_output(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    write_json(root, ".memory-index.json", {"l1_name": "on-demand.md"})
    assert run_build(str(root), False).code == 0
    assert (root / "on-demand.md").read_text(encoding="utf-8").startswith("---\nname: on-demand\n")
    assert run_build(str(root), True).code == 0
