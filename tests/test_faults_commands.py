"""DESIGN §7.2 and §8 for every other command that writes: migrate --apply, migrate --restore, demote and
suggest --apply. A failed write anywhere loses no text and leaves no temporary file, and running the
same command again (for migrate, the documented resume path) reaches the uninterrupted result."""

from __future__ import annotations

from pathlib import Path

import pytest
from faultkit import L0, Faults, Scenario, check_converges, fm_values, index_segments
from storekit import built_store, make_store, write_index, write_memory

from cc_memory_index.build import run_build, run_check
from cc_memory_index.demote import run_demote
from cc_memory_index.migrate import run_migrate
from cc_memory_index.migrate_snapshot import SNAPSHOT_PREFIX
from cc_memory_index.suggest import run_suggest

INDEX = """# Memory

Read this first.

## Rules
- [A](a.md) — first · [B](b.md) — second
- Ask before deleting anything
### Sub
- [A](a.md) — again · [Gone](gone.md) — a missing file
## Notes
- [C](c.md) and [B](b.md) — two links on one line
"""
RULES = """# Rules

- Ask before deleting anything
- Never push to main
"""
TIGHT = {"budget_bytes": 2048, "budget_lines": 20}


def _hand(base: Path, index: str = INDEX) -> Path:
    """A hand-written MEMORY.md and four memory files, one of them not in the index."""
    root = make_store(base)
    for name in ("a", "b", "c", "d"):
        write_memory(root, f"{name}.md", name=name, description=f"memory {name}", type="project")
    write_index(root, index)
    return root


def _rules_only(base: Path) -> Path:
    """An index that links no memory file: migrating it writes manual lines and no frontmatter."""
    return _hand(base, RULES)


def _migrated(base: Path) -> Path:
    root = _hand(base)
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and (root / ".index-render.json").is_file(), res.messages
    return root


def _original(root: Path) -> set[str]:
    return index_segments(INDEX) | fm_values(root)


def _apply(root: Path) -> int:
    return run_migrate(str(root), apply=True).code


def _restore(root: Path) -> int:
    snapshot = next(p.name for p in root.iterdir() if p.name.startswith(SNAPSHOT_PREFIX))
    return run_migrate(str(root), restore=snapshot[len(SNAPSHOT_PREFIX):]).code


def _over_budget(base: Path) -> Path:
    """Fourteen resident memories that do not fit TIGHT, built once (exit 1)."""
    root = make_store(base, TIGHT)
    for i in range(14):
        file = f"m{i:02d}.md"
        write_memory(root, file, name=file[:-3], type="reference", tier="resident",
                     idx=f"[{file[:-3]}]({file}) — " + "w" * (90 + 10 * i))
    assert run_build(str(root), False).code == 1
    return root


COMMANDS = [
    Scenario("migrate", _hand, _apply, _original),
    Scenario("migrate-rules-only", _rules_only, _apply, lambda root: index_segments(RULES) | fm_values(root)),
    Scenario("restore", _migrated, _restore, lambda root: index_segments(INDEX)),
    Scenario("demote", built_store, lambda root: run_demote(str(root), "build.md").code,
             lambda root: index_segments((root / L0).read_text(encoding="utf-8")) | {"Build", "make test"}),
    Scenario("suggest", _over_budget, lambda root: run_suggest(str(root), apply=True).code, fm_values),
]


@pytest.mark.parametrize("sc", COMMANDS, ids=[sc.name for sc in COMMANDS])
def test_command_converges_after_a_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sc: Scenario
) -> None:
    assert check_converges(tmp_path, Faults(monkeypatch), sc, seed=11) >= 4


def test_rerun_after_the_first_build_of_a_rules_only_migration_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Such a migration writes manual lines only, so no memory file has a tier. A rerun used to take
    the store for a hand-written one and migrate the generated MEMORY.md, link line included."""
    root = _rules_only(tmp_path)
    faults = Faults(monkeypatch)
    assert faults.run(lambda: _apply(root), 12, "EACCES") == 6 and faults.calls[-1] == ".index-render.json"
    res = run_migrate(str(root), apply=True)
    assert res.code == 0 and "never built" in res.messages[0], res.messages
    assert len([p for p in root.iterdir() if p.name.startswith(SNAPSHOT_PREFIX)]) == 1
    assert (root / L0).read_text(encoding="utf-8").count("Not loaded every session") == 1


def test_rerun_of_suggest_apply_builds_when_nothing_is_left_to_demote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _over_budget(tmp_path)
    faults = Faults(monkeypatch)
    assert faults.run(lambda: run_suggest(str(root), apply=True).code, 6, "ENOSPC") == 6
    assert faults.calls[-1] == "index-l1.md" and run_check(str(root)).code == 1
    res = run_suggest(str(root), apply=True)
    assert res.code == 0 and res.stats["demoted"] == [] and res.stats["build"]["overflow"] == 0
    assert run_check(str(root)).code == 0
