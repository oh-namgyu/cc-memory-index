from __future__ import annotations

import errno
import hashlib
import os
import stat
import types
from pathlib import Path
from typing import Callable, Iterator

import pytest

from cc_memory_index import fsprim
from cc_memory_index.fsprim import kind_of
from cc_memory_index.safeio import (
    ANY,
    ConcurrentEdit,
    Fingerprint,
    LockTimeout,
    RootChanged,
    SafeIOError,
    StoreDir,
    TooLarge,
    UnsafeEntry,
)

POSIX = os.name == "posix"
posix_only = pytest.mark.skipif(not POSIX, reason="POSIX only")
Symlink = Callable[[Path, Path], None]


def _fp(data: bytes) -> Fingerprint:
    return Fingerprint(len(data), hashlib.sha256(data).hexdigest())


@pytest.fixture(params=["dir_fd", "path"])
def sd(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StoreDir]:
    root = tmp_path / "store"
    root.mkdir()
    with StoreDir(str(root), force_path_mode=request.param == "path") as d:
        yield d


def _root(sd: StoreDir) -> Path:
    return Path(sd.path)


def _entries(sd: StoreDir) -> list[str]:
    return sorted(os.listdir(sd.path))


def test_errors_share_a_base_class() -> None:
    for cls in (UnsafeEntry, ConcurrentEdit, LockTimeout, TooLarge, RootChanged):
        assert issubclass(cls, SafeIOError)


def test_replace_writes_content_and_returns_fingerprint(sd: StoreDir) -> None:
    fp = sd.replace("a.md", b"hello", None)
    assert fp == _fp(b"hello")
    assert (_root(sd) / "a.md").read_bytes() == b"hello"
    assert sd.read("a.md") == (b"hello", fp)
    assert sd.fingerprint("a.md") == fp
    fp2 = sd.replace("a.md", b"world!", fp)
    assert sd.read("a.md") == (b"world!", fp2)
    sd.replace("a.md", b"any", ANY)
    assert (_root(sd) / "a.md").read_bytes() == b"any"
    assert _entries(sd) == ["a.md"]
    assert sd.names() == ["a.md"]


def test_fingerprint_and_kind_of_missing_entry(sd: StoreDir) -> None:
    assert sd.fingerprint("nope.md") is None
    assert sd.kind("nope.md") == "missing"
    with pytest.raises(FileNotFoundError):
        sd.read("nope.md")


@posix_only
def test_new_file_is_0600(sd: StoreDir) -> None:
    sd.replace("a.md", b"x", None)
    assert stat.S_IMODE(os.stat(_root(sd) / "a.md").st_mode) == 0o600


@posix_only
def test_replace_keeps_permission_bits(sd: StoreDir) -> None:
    path = _root(sd) / "a.md"
    path.write_bytes(b"x")
    os.chmod(path, 0o640)
    sd.replace("a.md", b"y", ANY)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640


def test_symlinked_target_is_refused_and_outside_file_unchanged(
    sd: StoreDir, tmp_path: Path, symlink: Symlink
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"secret")
    symlink(_root(sd) / "a.md", outside)
    assert sd.kind("a.md") == "link"
    for expect in (ANY, None, _fp(b"secret")):
        with pytest.raises(UnsafeEntry):
            sd.replace("a.md", b"evil", expect)
    with pytest.raises(UnsafeEntry):
        sd.remove("a.md", ANY)
    assert outside.read_bytes() == b"secret"
    assert (_root(sd) / "a.md").is_symlink()
    assert _entries(sd) == ["a.md"]


def test_hard_linked_target_is_refused(sd: StoreDir, tmp_path: Path) -> None:
    target = _root(sd) / "a.md"
    target.write_bytes(b"one")
    os.link(target, tmp_path / "other.md")
    assert sd.nlink("a.md") == 2
    with pytest.raises(UnsafeEntry):
        sd.replace("a.md", b"two", ANY)
    with pytest.raises(UnsafeEntry):
        sd.replace("a.md", b"two", _fp(b"one"))
    assert (tmp_path / "other.md").read_bytes() == b"one"
    assert sd.read("a.md")[0] == b"one"
    assert _entries(sd) == ["a.md"]


def test_reading_a_symlink_entry_is_refused(sd: StoreDir, tmp_path: Path, symlink: Symlink) -> None:
    (tmp_path / "outside.md").write_bytes(b"secret")
    symlink(_root(sd) / "a.md", tmp_path / "outside.md")
    with pytest.raises(UnsafeEntry):
        sd.read("a.md")
    with pytest.raises(UnsafeEntry):
        sd.fingerprint("a.md")


@posix_only
def test_o_nofollow_backs_up_the_kind_check(
    sd: StoreDir, tmp_path: Path, symlink: Symlink, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "outside.md").write_bytes(b"secret")
    symlink(_root(sd) / "a.md", tmp_path / "outside.md")
    monkeypatch.setattr(sd, "kind", lambda name: "file")
    with pytest.raises(UnsafeEntry):
        sd.read("a.md")


def test_directories_and_special_files_are_refused(sd: StoreDir) -> None:
    (_root(sd) / "d.md").mkdir()
    assert sd.kind("d.md") == "dir"
    with pytest.raises(UnsafeEntry):
        sd.read("d.md")
    with pytest.raises(UnsafeEntry):
        sd.replace("d.md", b"x", ANY)
    if hasattr(os, "mkfifo"):
        os.mkfifo(_root(sd) / "f.md")
        assert sd.kind("f.md") == "other"
        with pytest.raises(UnsafeEntry):
            sd.read("f.md")
        with pytest.raises(UnsafeEntry):
            sd.replace("f.md", b"x", ANY)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "..\\x", "a\x00b", "a.md:stream", ":"])
def test_names_must_be_plain(sd: StoreDir, name: str) -> None:
    for call in (lambda: sd.read(name), lambda: sd.kind(name), lambda: sd.replace(name, b"x", ANY)):
        with pytest.raises(UnsafeEntry):
            call()


def test_expect_must_be_fingerprint_none_or_any(sd: StoreDir) -> None:
    with pytest.raises(TypeError):
        sd.replace("a.md", b"x", "fp")  # type: ignore[arg-type]


def test_concurrent_edit_before_the_write(sd: StoreDir) -> None:
    sd.replace("a.md", b"v1", None)
    _, fp = sd.read("a.md")
    (_root(sd) / "a.md").write_bytes(b"v2")
    with pytest.raises(ConcurrentEdit):
        sd.replace("a.md", b"mine", fp)
    (_root(sd) / "a.md").write_bytes(b"longer v3")
    with pytest.raises(ConcurrentEdit):
        sd.replace("a.md", b"mine", fp)
    assert sd.read("a.md")[0] == b"longer v3"
    assert _entries(sd) == ["a.md"]


def test_concurrent_edit_between_the_two_checks(sd: StoreDir, monkeypatch: pytest.MonkeyPatch) -> None:
    sd.replace("a.md", b"v1", None)
    _, fp = sd.read("a.md")
    real = fsprim.write_all

    def racing(fd: int, data: bytes) -> None:
        real(fd, data)
        (_root(sd) / "a.md").write_bytes(b"v2")

    monkeypatch.setattr(fsprim, "write_all", racing)
    with pytest.raises(ConcurrentEdit):
        sd.replace("a.md", b"mine", fp)
    assert (_root(sd) / "a.md").read_bytes() == b"v2"
    assert _entries(sd) == ["a.md"]


def test_expect_none_but_the_file_appears(sd: StoreDir, monkeypatch: pytest.MonkeyPatch) -> None:
    (_root(sd) / "a.md").write_bytes(b"theirs")
    with pytest.raises(ConcurrentEdit):
        sd.replace("a.md", b"mine", None)
    real = fsprim.write_all

    def racing(fd: int, data: bytes) -> None:
        real(fd, data)
        (_root(sd) / "b.md").write_bytes(b"theirs")

    monkeypatch.setattr(fsprim, "write_all", racing)
    with pytest.raises(ConcurrentEdit):
        sd.replace("b.md", b"mine", None)
    assert (_root(sd) / "b.md").read_bytes() == b"theirs"
    assert _entries(sd) == ["a.md", "b.md"]


def test_expected_file_was_removed(sd: StoreDir) -> None:
    fp = sd.replace("a.md", b"v1", None)
    os.unlink(_root(sd) / "a.md")
    with pytest.raises(ConcurrentEdit):
        sd.replace("a.md", b"v2", fp)
    assert _entries(sd) == []


def test_temp_file_is_removed_when_os_replace_fails(sd: StoreDir, monkeypatch: pytest.MonkeyPatch) -> None:
    sd.replace("a.md", b"v1", None)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EIO, "simulated failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        sd.replace("a.md", b"v2", ANY)
    monkeypatch.undo()
    assert _entries(sd) == ["a.md"]
    assert sd.read("a.md")[0] == b"v1"


def test_too_large(sd: StoreDir) -> None:
    (_root(sd) / "a.md").write_bytes(b"x" * 11)
    with pytest.raises(TooLarge):
        sd.read("a.md", max_bytes=10)
    assert sd.read("a.md", max_bytes=11)[0] == b"x" * 11


def test_remove(sd: StoreDir) -> None:
    fp = sd.replace("a.md", b"v1", None)
    with pytest.raises(ConcurrentEdit):
        sd.remove("a.md", _fp(b"other"))
    sd.remove("a.md", fp)
    assert _entries(sd) == []
    sd.remove("a.md", ANY)
    sd.remove("a.md", None)
    with pytest.raises(ConcurrentEdit):
        sd.remove("a.md", fp)


def _fake_stat(mode: int, attrs: int = 0, tag: int = 0) -> os.stat_result:
    return types.SimpleNamespace(st_mode=mode, st_file_attributes=attrs, st_reparse_tag=tag)  # type: ignore[return-value]


def test_kind_of_reparse_points() -> None:
    reparse = stat.FILE_ATTRIBUTE_REPARSE_POINT
    junction = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT, a name surrogate
    cloud = 0x9000001A  # IO_REPARSE_TAG_CLOUD_A, not a name surrogate
    assert kind_of(_fake_stat(stat.S_IFDIR | 0o755, reparse, junction)) == "link"
    assert kind_of(_fake_stat(stat.S_IFLNK | 0o777, reparse, 0xA000000C)) == "link"
    assert kind_of(_fake_stat(stat.S_IFREG | 0o644, reparse, cloud)) == "file"
    assert kind_of(_fake_stat(stat.S_IFREG | 0o644)) == "file"
