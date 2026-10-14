"""DESIGN §6.1, §7.1-§7.2 for build: a write that fails at any point leaves a valid store that has lost
no text and holds no temporary file, and running build again ends in exactly the state an
uninterrupted build reaches, even when that rerun fails once more on the way."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest
from faultkit import L0, L1, Faults, Scenario, check_converges, fm_values
from storekit import append, built_store, claude_adds_memory, write_json, write_memory

from cc_memory_index.build import run_build
from cc_memory_index.items import PENDING_NAME

RESCUED = {"free text added first", "[New note](new_note.md) — why it matters", "[Second](second.md) — added to L1"}
WAITING = "[Waiting](waiting.md) — rescued before the crash"
CONFLICT = "[Pinned](pinned.md) — a conflicting line"
FORCED = {"# My own title", "[Tea](tea.md) and [Old](old.md) — one line, two files"}


def _build(force: bool = False) -> Callable[[Path], int]:
    return lambda root: run_build(str(root), force).code


def _plain(base: Path) -> Path:
    """Built, then a memory added and another one's tier changed by hand: L1, L0 and the record change."""
    root = built_store(base)
    write_memory(root, "fresh.md", name="Fresh", description="added since", type="project", tier="resident")
    write_memory(root, "tea.md", name="Tea", description="likes tea", type="user")
    return root


def rescue_store(base: Path) -> Path:
    """A manual line and a line for a new memory added to MEMORY.md, a line for another one to L1."""
    root = built_store(base)
    append(root, L0, "- free text added first\n")
    claude_adds_memory(root, "new_note.md")
    write_memory(root, "second.md", name="Second", type="feedback")
    append(root, L1, "- [Second](second.md) — added to L1\n")
    return root


def _entry(file: str, idx: str) -> dict[str, object]:
    return {"file": file, "idx": idx, "section": "", "sub": "", "order": 1000001}


def _fold(base: Path) -> Path:
    """A rescued line left in .index-pending.json by a run that stopped before folding it, and a
    conflict (DESIGN §6.2): a pending line for a memory that has gained another idx since."""
    root = built_store(base)
    write_memory(root, "waiting.md", name="Waiting", description="not folded yet", type="project")
    write_memory(root, "pinned.md", name="Pinned", type="project", idx="[Pinned](pinned.md) — its own line")
    write_json(root, PENDING_NAME, {"version": 1, "entries": [_entry("waiting.md", WAITING),
                                                              _entry("pinned.md", CONFLICT)]})
    return root


def _force(base: Path) -> Path:
    """Unrescuable edits of both generated files, five older backups of MEMORY.md, and a pending line."""
    root = _fold(base)
    l0 = (root / L0).read_text(encoding="utf-8")
    (root / L0).write_text(l0.replace("# Memory Index", "# My own title", 1), encoding="utf-8")
    append(root, L1, "- [Tea](tea.md) and [Old](old.md) — one line, two files\n")
    for i in range(5):
        (root / f".MEMORY.md.bak-2020010{i + 1}T000000Z").write_text(f"backup {i}\n", encoding="utf-8")
    return root


BUILDS = [
    Scenario("plain", _plain, _build(), fm_values),
    Scenario("rescue", rescue_store, _build(), lambda root: fm_values(root) | RESCUED),
    Scenario("fold", _fold, _build(), lambda root: fm_values(root) | {WAITING, CONFLICT}),
    Scenario("force", _force, _build(force=True), lambda root: fm_values(root) | FORCED),
]


@pytest.mark.parametrize("sc", BUILDS, ids=[sc.name for sc in BUILDS])
def test_build_converges_after_a_failed_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sc: Scenario) -> None:
    assert check_converges(tmp_path, Faults(monkeypatch), sc, seed=7) >= 3


def test_rerun_of_force_reuses_the_backup_it_made(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure after the MEMORY.md backup was written: the rerun keeps that backup instead of
    writing a second copy, which would also have pruned one more of the older backups."""
    (tmp_path / "s").mkdir()
    root = _force(tmp_path / "s")
    faults = Faults(monkeypatch)
    assert faults.run(lambda: run_build(str(root), True).code, 3, "ENOSPC") == 6
    first = faults.calls[0]
    assert faults.run(lambda: run_build(str(root), True).code) == 0 and first not in faults.calls
    backups = sorted(p.name for p in root.iterdir() if p.name.startswith(".MEMORY.md.bak-"))
    assert backups == sorted([first] + [f".MEMORY.md.bak-2020010{i}T000000Z" for i in range(2, 6)])
