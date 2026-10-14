"""Fault injection and store comparison for the write-safety tests (DESIGN §7.1-§7.3)."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import random
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Any, Callable, Iterator, Optional

import pytest

from cc_memory_index import fsprim
from cc_memory_index.frontmatter import parse
from cc_memory_index.items import MANUAL_NAME, PENDING_NAME
from cc_memory_index.safeio import StoreDir
from cc_memory_index.text import normalize_ws, split_segments

L0, L1 = "MEMORY.md", "index-l1.md"
KINDS = ("ENOSPC", "EACCES")
TEMP = re.compile(r"\..+\.tmp-[0-9a-f]{16}\Z")
STAMPED = re.compile(r"(\.index-backup-|\.bak-)([0-9]{8}T[0-9]{6}Z(?:-[0-9]{1,6})?)\Z")


def _error(kind: str) -> OSError:
    if kind == "ENOSPC":
        return OSError(errno.ENOSPC, "No space left on device (injected)")
    return PermissionError(errno.EACCES, "Permission denied (injected)")


@contextlib.contextmanager
def swapped(owner: Any, attr: str, replacement: Callable[..., Any]) -> Iterator[None]:
    """Replace owner.attr for the duration of the block."""
    saved = getattr(owner, attr)
    setattr(owner, attr, replacement)
    try:
        yield
    finally:
        setattr(owner, attr, saved)


class Faults:
    """Counts StoreDir.replace and StoreDir.remove calls; call number fail_at fails inside the real method.

    ENOSPC fails while the temporary file is written (half of it is), EACCES fails the rename
    (PermissionError, as on Windows for a file another process holds open): the temporary file
    exists in both cases and must be discarded. A failing remove fails its unlink.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        self.fail_at: Optional[int] = None
        self.kind = KINDS[0]
        self.fired = False
        real_replace, real_remove = StoreDir.replace, StoreDir.remove

        def replace(sd: StoreDir, name: str, data: bytes, expect: Any) -> Any:
            with self._step(name, "replace"):
                return real_replace(sd, name, data, expect)

        def remove(sd: StoreDir, name: str, expect: Any) -> None:
            with self._step(name, "remove"):
                real_remove(sd, name, expect)

        monkeypatch.setattr(StoreDir, "replace", replace)
        monkeypatch.setattr(StoreDir, "remove", remove)

    @contextlib.contextmanager
    def _step(self, name: str, op: str) -> Iterator[None]:
        self.calls.append(name)
        if len(self.calls) != self.fail_at:
            yield
            return
        error = _error(self.kind)

        def fail(*args: Any, **kwargs: Any) -> None:
            self.fired = True
            raise error

        def half_then_fail(fd: int, data: bytes) -> None:
            os.write(fd, data[: len(data) // 2])
            fail()

        if op == "remove":
            patched = swapped(os, "unlink", fail)
        elif self.kind == "ENOSPC":
            patched = swapped(fsprim, "write_all", half_then_fail)
        else:
            patched = swapped(os, "replace", fail)
        with patched:
            yield

    def run(self, command: Callable[[], int], fail_at: Optional[int] = None, kind: str = KINDS[0]) -> int:
        self.calls, self.fail_at, self.kind, self.fired = [], fail_at, kind, False
        return command()


def tree(root: Path, known: AbstractSet[str] = frozenset()) -> list[tuple[str, bytes]]:
    """Every file under root (the lock file left out) as sorted (path, bytes).

    The timestamp in the name of a snapshot directory or a --force backup that is not in known
    (made by the run itself) is blanked, and so is the snapshot id in its manifest, so that runs
    made at different times compare equal; a duplicate still shows as a second entry.
    """
    out: list[tuple[str, bytes]] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or path.is_symlink() or rel == ".index.lock":
            continue
        data = path.read_bytes()
        top, _, rest = rel.partition("/")
        m = STAMPED.search(top)
        if m and top not in known:
            if rest == "manifest.json":
                data = data.replace(m.group(2).encode(), b"ID")
            top = top[: m.start(2)] + "NOW"
        out.append(("/".join(filter(None, (top, rest))), data))
    return sorted(out)


def temp_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if TEMP.match(p.name))


def _strings(obj: object) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _strings(value)


def _values(text: str) -> list[str]:
    fm = parse(text)
    return [normalize_ws(v) for v in [*fm.top.values(), *fm.metadata.values()] if v.strip()]


def fm_values(root: Path) -> set[str]:
    """Every frontmatter value of every memory file directly in root."""
    out: set[str] = set()
    for path in root.glob("*.md"):
        if path.name not in (L0, L1) and path.is_file():
            out.update(_values(path.read_text(encoding="utf-8")))
    return out


def index_segments(text: str) -> set[str]:
    """The segments of an index file's list lines and of its other non-heading lines."""
    out: set[str] = set()
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("- "):
            out.update(normalize_ws(p) for p in split_segments(s[2:]))
        elif s and not s.startswith("## ") and s != "---":
            out.add(normalize_ws(s))
    return out


def _data_texts(root: Path) -> set[str]:
    """The manual preamble lines and manual lines, and the pending lines."""
    out: set[str] = set()
    for name, keys in ((MANUAL_NAME, ("seg",)), (PENDING_NAME, ("idx",))):
        if (root / name).is_file():
            obj = json.loads((root / name).read_text(encoding="utf-8"))
            out.update(normalize_ws(line) for line in (obj.get("preamble") or "").split("\n") if line.strip())
            out.update(normalize_ws(e[k]) for e in obj.get("lines", obj.get("entries", [])) for k in keys)
    return out


def texts(root: Path) -> set[str]:
    """What a person wrote or reads: every segment of MEMORY.md and L1, every frontmatter value,
    every manual and pending line."""
    found = fm_values(root) | _data_texts(root)
    for name in (L0, L1):
        if (root / name).is_file():
            found |= index_segments((root / name).read_text(encoding="utf-8"))
    return found


def corpus(root: Path) -> str:
    """All text under root, JSON strings and frontmatter values decoded, whitespace-normalised."""
    parts: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        text = path.read_bytes().decode("utf-8", "replace")
        parts.append(normalize_ws(text))
        if path.suffix == ".json":
            with contextlib.suppress(ValueError):
                parts += [normalize_ws(s) for s in _strings(json.loads(text))]
        else:
            parts += _values(text)
    return "\n".join(parts)


def assert_safe(root: Path, keep: AbstractSet[str], where: str) -> None:
    """No temporary file is left, and every text that must be kept is still somewhere in the store."""
    assert temp_files(root) == [], where
    found = corpus(root)
    lost = sorted(t for t in keep if t not in found)
    assert lost == [], f"{where}: lost {lost[:3]}"


@dataclass(frozen=True)
class Scenario:
    """A fixed starting store, the command under test, and texts it must keep that the start shows.

    The texts checked are those of the start that are still somewhere in the store after an
    uninterrupted run; precious names texts that must be among them (hand-added lines, the original
    index), so that the check is never empty where it matters.
    """

    name: str
    make: Callable[[Path], Path]
    run: Callable[[Path], int]
    precious: Callable[[Path], set[str]]


def copy_store(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, symlinks=True)
    return dst


def check_converges(tmp_path: Path, faults: Faults, sc: Scenario, seed: int) -> int:
    """Fail every write of sc in turn with each error kind; rerun, failing once more at a seeded write
    of the rerun, then cleanly; compare with an uninterrupted run. Returns the number of writes."""
    (tmp_path / "start").mkdir()
    start = sc.make(tmp_path / "start")
    known = frozenset(os.listdir(start))
    clean = copy_store(start, tmp_path / "clean")
    code = faults.run(lambda: sc.run(clean))
    names, expected = list(faults.calls), tree(clean, known)
    found = corpus(clean)
    keep = {t for t in texts(start) if t in found}
    assert sc.precious(start) <= keep, sorted(sc.precious(start) - keep)[:3]
    assert names and temp_files(clean) == []
    rng = random.Random(seed)
    for k, kind in [(k, kind) for k in range(1, len(names) + 1) for kind in KINDS]:
        where = f"{sc.name}: {kind} at write {k} of {names}"
        root = copy_store(start, tmp_path / f"{kind}-{k}")
        assert faults.run(lambda: sc.run(root), k, kind) == 6 and faults.fired and len(faults.calls) == k, where
        assert_safe(root, keep, where)
        probe = copy_store(root, tmp_path / f"{kind}-{k}-probe")
        assert faults.run(lambda: sc.run(probe)) == code, where
        assert tree(probe, known) == expected, where
        if faults.calls:
            j, other = rng.randint(1, len(faults.calls)), KINDS[1 - KINDS.index(kind)]
            where += f", then {other} at write {j} of the rerun {faults.calls}"
            assert faults.run(lambda: sc.run(root), j, other) == 6, where
            assert_safe(root, keep, where)
            assert faults.run(lambda: sc.run(root)) == code, where
            assert tree(root, known) == expected, where
        shutil.rmtree(probe)
        shutil.rmtree(root)
    return len(names)
