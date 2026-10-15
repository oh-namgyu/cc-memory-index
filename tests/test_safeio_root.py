from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Callable, Iterator

import pytest

from cc_memory_index import fsprim
from cc_memory_index.safeio import ANY, RootChanged, StoreDir, UnsafeEntry

POSIX = os.name == "posix"
posix_only = pytest.mark.skipif(not POSIX, reason="POSIX only")
Symlink = Callable[[Path, Path], None]


@pytest.fixture(params=["dir_fd", "path"])
def sd(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StoreDir]:
    root = tmp_path / "store"
    root.mkdir()
    with StoreDir(str(root), force_path_mode=request.param == "path") as d:
        yield d


def _root(sd: StoreDir) -> Path:
    return Path(sd.path)


def test_dir_fd_mode_is_the_default_on_posix(tmp_path: Path) -> None:
    assert fsprim.DIR_FD is POSIX
    with StoreDir(str(tmp_path)) as d:
        assert (d._fd is not None) is POSIX
    with StoreDir(str(tmp_path), force_path_mode=True) as d:
        assert d._fd is None


def test_root_must_be_a_directory(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        StoreDir(str(tmp_path / "f"))


def test_subdir(sd: StoreDir, tmp_path: Path, symlink: Symlink) -> None:
    with sd.subdir("bak", create=True) as sub:
        sub.replace("x.md", b"1", None)
    assert (_root(sd) / "bak" / "x.md").read_bytes() == b"1"
    if POSIX:
        assert stat.S_IMODE(os.stat(_root(sd) / "bak").st_mode) == 0o700
    with sd.subdir("bak", create=False) as again:
        assert again.names() == ["x.md"]
    with pytest.raises(FileNotFoundError):
        sd.subdir("missing", create=False)
    (_root(sd) / "file").write_bytes(b"x")
    with pytest.raises(UnsafeEntry):
        sd.subdir("file", create=True)
    (tmp_path / "elsewhere").mkdir()
    symlink(_root(sd) / "linked", tmp_path / "elsewhere")
    with pytest.raises(UnsafeEntry):
        sd.subdir("linked", create=True)
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_closed_store_refuses_operations(tmp_path: Path) -> None:
    d = StoreDir(str(tmp_path))
    d.close()
    d.close()
    with pytest.raises(ValueError):
        d.kind("a.md")
    with pytest.raises(ValueError):
        d.names()


@posix_only
def test_dir_fd_mode_writes_into_the_original_root_after_a_swap(tmp_path: Path) -> None:
    root, moved = tmp_path / "store", tmp_path / "moved"
    root.mkdir()
    (root / "a.md").write_bytes(b"old")
    with StoreDir(str(root)) as d:
        _, fp = d.read("a.md")
        os.rename(root, moved)
        root.mkdir()
        d.replace("a.md", b"new", fp)
        d.replace("b.md", b"b", None)
    assert (moved / "a.md").read_bytes() == b"new"
    assert sorted(os.listdir(moved)) == ["a.md", "b.md"]
    assert os.listdir(root) == []


def test_path_mode_detects_a_swapped_root(tmp_path: Path) -> None:
    root, moved = tmp_path / "store", tmp_path / "moved"
    root.mkdir()
    (root / "a.md").write_bytes(b"old")
    with StoreDir(str(root), force_path_mode=True) as d:
        _, fp = d.read("a.md")
        os.rename(root, moved)
        root.mkdir()
        with pytest.raises(RootChanged):
            d.replace("a.md", b"new", fp)
        with pytest.raises(RootChanged):
            d.replace("b.md", b"b", None)
    assert (moved / "a.md").read_bytes() == b"old"
    assert os.listdir(moved) == ["a.md"]
    assert os.listdir(root) == []


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_windows_junction_is_a_link(tmp_path: Path) -> None:
    import _winapi

    root, target = tmp_path / "store", tmp_path / "target"
    root.mkdir()
    target.mkdir()
    (target / "a.md").write_bytes(b"secret")
    _winapi.CreateJunction(str(target), str(root / "j.md"))
    with StoreDir(str(root)) as d:
        assert d.kind("j.md") == "link"
        with pytest.raises(UnsafeEntry):
            d.replace("j.md", b"x", ANY)
        with pytest.raises(UnsafeEntry):
            d.subdir("j.md", create=False)
    assert (target / "a.md").read_bytes() == b"secret"
    assert os.listdir(target) == ["a.md"]
