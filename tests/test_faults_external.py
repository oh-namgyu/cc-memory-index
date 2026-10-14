"""DESIGN §7.3: Claude Code's Write and Edit tools do not take this tool's lock. An edit that lands
after a file was read, or while its replacement is being written (right before the final check
and the rename), is detected: the command exits 4, the edit stays exactly as the editor left it,
and running the command again converges.

What lands between that final check and os.replace itself is not detected, as §7.3 says: no
stdlib call renames only if the target is unchanged. test_concurrency.py measures that window.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest
from faultkit import L0, L1, assert_safe, swapped
from storekit import built_store
from test_faults import RESCUED, rescue_store

from cc_memory_index import fsprim
from cc_memory_index.build import run_build, run_check
from cc_memory_index.demote import run_demote
from cc_memory_index.safeio import StoreDir

EDITED_LINE = "a line saved by another session"
EDITED_IDX = "[Edited](edited.md) — an idx set by the editor"


def _append(path: Path) -> str:
    """An in-place append, as an editor that writes the file it opened."""
    line = f"- {EDITED_LINE}\n" if path.name in (L0, L1) else f"{EDITED_LINE}\n"
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(line)
    return EDITED_LINE


def _rewrite(path: Path) -> str:
    """The whole file saved through a temporary file and a rename, as many editors do: a memory
    file gets a new idx, a generated file a line added at the end."""
    text = path.read_text(encoding="utf-8")
    if path.name in (L0, L1):
        text, mark = text + f"- {EDITED_LINE}\n", EDITED_LINE
    else:
        assert "\nmetadata:\n" in text
        text, mark = text.replace("\nmetadata:\n", f'\nmetadata:\n  idx: "{EDITED_IDX}"\n', 1), EDITED_IDX
    tmp = path.with_name(path.name + ".editor-save")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return mark


@dataclass
class Editor:
    """Edits target once, at the chosen moment of a run, and remembers the result."""

    root: Path
    target: str
    edit: Callable[[Path], str]
    fired: bool = False
    mark: str = ""
    result: tuple[bytes, int] = (b"", 0)

    def now(self) -> None:
        self.fired = True
        path = self.root / self.target
        self.mark = self.edit(path)
        self.result = (path.read_bytes(), os.stat(path).st_ino)

    def after_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = StoreDir.read

        def read(sd: StoreDir, name: str, *args: Any) -> Any:
            out = real(sd, name, *args)
            if name == self.target and not self.fired:
                self.now()
            return out

        monkeypatch.setattr(StoreDir, "read", read)

    def before_rename(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """While the replacement of target is written: after the first check, before the final one."""
        real, real_write = StoreDir.replace, fsprim.write_all

        def write_all(fd: int, data: bytes) -> None:
            real_write(fd, data)
            self.now()

        def replace(sd: StoreDir, name: str, data: bytes, expect: Any) -> Any:
            if name != self.target or self.fired:
                return real(sd, name, data, expect)
            with swapped(fsprim, "write_all", write_all):
                return real(sd, name, data, expect)

        monkeypatch.setattr(StoreDir, "replace", replace)


def _demote(root: Path) -> int:
    return run_demote(str(root), "build.md").code


def _build(root: Path) -> int:
    return run_build(str(root), False).code


TARGETS = {"build": ("new_note.md", L0, L1), "demote": ("build.md", L0, L1)}
CASES = [(command, target) for command, targets in TARGETS.items() for target in targets]


@pytest.mark.parametrize("edit", [_append, _rewrite], ids=["append", "rewrite"])
@pytest.mark.parametrize("moment", ["after_read", "before_rename"])
@pytest.mark.parametrize("command, target", CASES)
def test_external_edit_is_kept_and_a_rerun_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, target: str, moment: str,
    edit: Callable[[Path], str],
) -> None:
    if command == "build":
        (tmp_path / "s").mkdir()
        root, run, keep = rescue_store(tmp_path / "s"), _build, set(RESCUED)
    else:
        root, run, keep = built_store(tmp_path), _demote, {"Build", "make test", "reference"}
    editor = Editor(root, target, edit)
    with monkeypatch.context() as m:
        getattr(editor, moment)(m)
        assert run(root) == 4 and editor.fired
    path = root / target
    assert (path.read_bytes(), os.stat(path).st_ino) == editor.result, "the edit was written over"
    assert_safe(root, keep | {editor.mark}, f"{command} {target} {moment}")
    assert run(root) == 0
    assert run_check(str(root)).code == 0
    settled = run_build(str(root), False)
    assert (settled.code, settled.stats["written"]) == (0, [])
    assert_safe(root, keep | {editor.mark}, "after the rerun")
