"""DESIGN §6.1 and §7.2: a crash at any write of the rescue path loses nothing, and the next build
ends in exactly the state an uninterrupted build would have reached."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Optional

import pytest
from storekit import append, built_store, claude_adds_memory, contents, write_memory

from cc_memory_index.build import run_build
from cc_memory_index.items import MANUAL_NAME, PENDING_NAME
from cc_memory_index.safeio import StoreDir

L0, L1, MARK = "MEMORY.md", "index-l1.md", ".index-render.json"
RESCUE_PATH = [PENDING_NAME, MANUAL_NAME, "new_note.md", "second.md", PENDING_NAME, L1, L0, MARK]


class _Faults:
    """Counts StoreDir.replace calls and raises OSError on call number fail_at."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        self.fail_at: Optional[int] = None
        real = StoreDir.replace

        def replace(sd: StoreDir, name: str, data: bytes, expect: Any) -> Any:
            self.calls.append(name)
            if len(self.calls) == self.fail_at:
                raise OSError(f"injected crash at write {self.fail_at}")
            return real(sd, name, data, expect)

        monkeypatch.setattr(StoreDir, "replace", replace)

    def run(self, root: Path, fail_at: Optional[int]) -> int:
        self.calls, self.fail_at = [], fail_at
        return run_build(str(root), False).code


def _edits_before_any_build(root: Path) -> None:
    """A manual line first, then a line for a new file in L0 and one in L1."""
    append(root, L0, "- free text added first\n")
    claude_adds_memory(root, "new_note.md")
    write_memory(root, "second.md", name="Second", type="feedback")
    append(root, L1, "- [Second](second.md) — added to L1\n")


def _edits_after_the_hook_built(root: Path) -> None:
    """As the hook sees it: the new files were built (default lines in L1) before the index edits."""
    write_memory(root, "new_note.md", name="New note", description="a new memory", type="project")
    write_memory(root, "second.md", name="Second", type="feedback")
    assert run_build(str(root), False).code == 0
    append(root, L0, "- free text added first\n- [New note](new_note.md) — why it matters\n")
    append(root, L1, "- [Second](second.md) — added to L1\n")


@pytest.mark.parametrize("edit", [_edits_before_any_build, _edits_after_the_hook_built])
def test_crash_at_every_write_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit: Callable[[Path], None]
) -> None:
    start = built_store(tmp_path)
    edit(start)
    faults = _Faults(monkeypatch)
    clean = tmp_path / "clean"
    shutil.copytree(start, clean)
    assert faults.run(clean, None) == 0 and faults.calls == RESCUE_PATH
    expected = contents(clean)
    for k in range(1, len(RESCUE_PATH) + 1):
        root = tmp_path / f"crash-{k}"
        shutil.copytree(start, root)
        assert faults.run(root, k) == 6 and len(faults.calls) == k
        assert faults.run(root, None) == 0, f"rerun after a crash at write {k}"
        assert contents(root) == expected, f"crash at write {k} ({RESCUE_PATH[k - 1]})"
        assert faults.run(root, None) == 0 and faults.calls == []
