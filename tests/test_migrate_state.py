"""Which stores need migrating, and odd byte order marks in a hand-written MEMORY.md (DESIGN §8)."""

from __future__ import annotations

import json
from pathlib import Path

from storekit import make_store, snapshot, write_index, write_memory
from test_migrate import L0, L1, MANUAL, _store

from cc_memory_index.build import run_build
from cc_memory_index.migrate import run_migrate
from cc_memory_index.render import link_line


def test_a_tier_set_by_hand_does_not_skip_the_migration(tmp_path: Path) -> None:
    """The README scenario: a.md has a tier, b.md has none, and MEMORY.md was written by hand. Only
    the render record or the manual-lines file says a store was migrated."""
    root = make_store(tmp_path)
    write_memory(root, "a.md", name="a", tier="resident")
    write_memory(root, "b.md", name="b")
    write_index(root, "# Mine\n\n## S\n- [A](a.md) — first\n- [B](b.md) — second\n")
    before = snapshot(root)
    assert run_build(str(root), False).code == 2 and snapshot(root) == before
    dry = run_migrate(str(root))
    assert dry.code == 0 and "already" not in dry.stats and dry.stats["resident"] == 2 and snapshot(root) == before
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and "snapshot" in res.stats, res.messages
    assert json.loads((root / MANUAL).read_text(encoding="utf-8")) == {"version": 1, "preamble": "# Mine", "lines": []}
    assert "- [A](a.md) — first\n- [B](b.md) — second\n" in (root / L0).read_text(encoding="utf-8")
    assert run_build(str(root), False).stats["written"] == []


def test_apply_always_writes_the_manual_lines_file(tmp_path: Path) -> None:
    root = _store(tmp_path, "## S\n- [A](a.md) — x\n", "a.md")
    assert run_migrate(str(root), apply=True).code == 0
    assert json.loads((root / MANUAL).read_text(encoding="utf-8")) == {"version": 1, "preamble": None, "lines": []}
    (root / ".index-render.json").unlink()
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and res.stats["already"] == "unbuilt" and (root / ".index-render.json").is_file()


def test_a_doubled_bom_is_one_bom_and_text(tmp_path: Path) -> None:
    """Audit: a MEMORY.md starting with two BOMs made migrate --apply write, then exit 8 on every rerun."""
    root = _store(tmp_path, "\ufeff\ufeff# Memory\n\n## S\n- [A](a.md) — x\n", "a.md")
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and not (root / ".index-migrating").exists(), res.messages
    assert (root / L0).read_text(encoding="utf-8").startswith("# Memory\n\n" + link_line(L1))
    assert run_migrate(str(root), apply=True).stats["already"] == "built"
    assert run_build(str(root), False).stats["written"] == []


def test_a_preamble_line_that_is_only_a_bom(tmp_path: Path) -> None:
    """Audit: such a line made even build --force exit 8."""
    root = _store(tmp_path, "## S\n- [A](a.md) — x\n", "a.md")
    assert run_migrate(str(root), apply=True).code == 0
    (root / MANUAL).write_text(json.dumps({"version": 1, "preamble": "# Mine\n\ufeff\nmore", "lines": []}),
                               encoding="utf-8")
    assert run_build(str(root), True).code == 0
    assert (root / L0).read_text(encoding="utf-8").startswith("# Mine\n\nmore\n\n" + link_line(L1))
    assert run_build(str(root), False).stats["written"] == []
