"""Store-relative file access that never follows links (DESIGN §5.2, §7.1, §7.3-§7.5)."""

from __future__ import annotations

import contextlib
import errno
import os
import re
import secrets
import stat
import time
from typing import Iterator, Optional

from . import fsprim
from .fsprim import BINARY, NOFOLLOW, NONBLOCK, kind_of
from .safetypes import (
    ANY, ConcurrentEdit, Expect, Fingerprint, LockTimeout, RootChanged, SafeIOError, TooLarge, UnsafeEntry,
)

__all__ = ["ANY", "LOCK_NAME", "TEMP_MIN_AGE_NS", "TEMP_NAME", "ConcurrentEdit", "Expect", "Fingerprint", "LockTimeout",
           "RootChanged", "SafeIOError", "StoreDir", "TooLarge", "UnsafeEntry"]
LOCK_NAME = ".index.lock"
TEMP_NAME = re.compile(r"\.(.+)\.tmp-[0-9a-f]{16}\Z")
TEMP_MIN_AGE_NS = 10 * 60 * 10**9


def _check_name(name: str) -> None:
    if not isinstance(name, str) or name in ("", ".", "..") or any(c in name for c in "/\\\x00"):
        raise UnsafeEntry(f"not a plain entry name: {name!r}")


def _check_expect(expect: Expect) -> None:
    if not (expect is None or expect is ANY or isinstance(expect, Fingerprint)):
        raise TypeError("expect must be a Fingerprint, None or ANY")


class StoreDir:
    """A store root resolved and opened once; entries are addressed by plain names only.

    On POSIX every operation is relative to the root's directory descriptor, so moving or
    replacing the root path cannot redirect it. Elsewhere (or with force_path_mode) paths are
    joined and the root's identity is re-checked before each operation.
    """

    def __init__(self, path: str, force_path_mode: bool = False) -> None:
        self.path = os.path.realpath(path)
        st = os.stat(self.path)
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", self.path)
        self._ident = (st.st_dev, st.st_ino)
        self._fd: Optional[int] = None
        self._closed = False
        if fsprim.DIR_FD and not force_path_mode:
            fd = os.open(self.path, os.O_RDONLY | fsprim.DIRECTORY)
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != self._ident:
                os.close(fd)
                raise RootChanged(f"{self.path} changed while it was being opened")
            self._fd = fd

    def __enter__(self) -> "StoreDir":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def _check_root(self) -> None:
        if self._closed:
            raise ValueError("StoreDir is closed")
        if self._fd is not None:
            return
        try:
            st = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError as e:
            raise RootChanged(f"{self.path} no longer exists") from e
        if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != self._ident:
            raise RootChanged(f"{self.path} was replaced during the run")

    def _at(self, name: str) -> tuple[str, dict[str, int]]:
        """Path argument and dir_fd keyword for one entry."""
        _check_name(name)
        self._check_root()
        if self._fd is not None:
            return name, {"dir_fd": self._fd}
        return os.path.join(self.path, name), {}

    def _lstat(self, name: str) -> os.stat_result:
        target, kw = self._at(name)
        return os.stat(target, follow_symlinks=False, **kw)

    def _open(self, name: str, flags: int, mode: int = 0o600) -> int:
        target, kw = self._at(name)
        try:
            return os.open(target, flags | NOFOLLOW | BINARY, mode, **kw)
        except OSError as e:
            if NOFOLLOW and e.errno in fsprim.LINK_ERRNOS:
                raise UnsafeEntry(f"{name} is a symbolic link") from e
            raise

    def _open_regular(self, name: str) -> int:
        found = self.kind(name)
        if found == "missing":
            raise FileNotFoundError(errno.ENOENT, "no such entry", name)
        if found != "file":
            raise UnsafeEntry(f"{name} is not a regular file ({found})")
        fd = self._open(name, os.O_RDONLY | NONBLOCK)
        try:
            if kind_of(os.fstat(fd)) == "file":
                return fd
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        raise UnsafeEntry(f"{name} is not a regular file")

    def names(self) -> list[str]:
        self._check_root()
        return sorted(os.listdir(self.path if self._fd is None else self._fd))

    def kind(self, name: str) -> str:
        """One of 'file', 'link', 'dir', 'other', 'missing'; links are never followed."""
        try:
            return kind_of(self._lstat(name))
        except FileNotFoundError:
            return "missing"

    def nlink(self, name: str) -> int:
        return self._lstat(name).st_nlink

    def entry_stat(self, name: str) -> tuple[str, int, int]:
        """(kind, size, mtime_ns) of an entry from one lstat; links are never followed."""
        st = self._lstat(name)
        return kind_of(st), st.st_size, st.st_mtime_ns

    def read(self, name: str, max_bytes: int = 4 * 1024 * 1024) -> tuple[bytes, Fingerprint]:
        data = b"".join(self.iter_chunks(name, fsprim.CHUNK, max_bytes))
        return data, Fingerprint(*fsprim.digest([data]))

    def iter_chunks(self, name: str, chunk: int, max_bytes: int) -> Iterator[bytes]:
        """Stream an entry with read's refusals; TooLarge once more than max_bytes were read."""
        fd = self._open_regular(name)
        try:
            size = 0
            for piece in fsprim.stream(fd, chunk, max_bytes):
                size += len(piece)
                if size > max_bytes:
                    raise TooLarge(f"{name} is larger than {max_bytes} bytes")
                yield piece
        finally:
            os.close(fd)

    def fingerprint(self, name: str) -> Optional[Fingerprint]:
        try:
            return Fingerprint(*fsprim.digest(self.iter_chunks(name, fsprim.CHUNK, fsprim.NO_LIMIT)))
        except FileNotFoundError:
            return None

    def _verify(self, name: str, expect: Expect) -> Optional[os.stat_result]:
        """Refuse unsafe targets and changed content; returns the target's lstat or None."""
        try:
            st: Optional[os.stat_result] = self._lstat(name)
        except FileNotFoundError:
            st = None
        if st is None:
            if expect is None or expect is ANY:
                return None
            raise ConcurrentEdit(f"{name} was removed after it was read")
        if kind_of(st) != "file":
            raise UnsafeEntry(f"{name} is not a regular file ({kind_of(st)})")
        if st.st_nlink > 1:
            raise UnsafeEntry(f"{name} has {st.st_nlink} hard links")
        if expect is None:
            raise ConcurrentEdit(f"{name} appeared after it was checked")
        if isinstance(expect, Fingerprint) and (st.st_size != expect.size or self.fingerprint(name) != expect):
            raise ConcurrentEdit(f"{name} was edited after it was read")
        return st

    def replace(self, name: str, data: bytes, expect: Expect) -> Fingerprint:
        """Atomically replace one entry; expect is its Fingerprint, None (must not exist) or ANY."""
        _check_expect(expect)
        st = self._verify(name, expect)
        tmp = f".{name}.tmp-{secrets.token_hex(8)}"
        fd = self._open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            try:
                fsprim.write_all(fd, data)
                if st is not None and hasattr(os, "fchmod"):
                    os.fchmod(fd, stat.S_IMODE(st.st_mode))
                os.fsync(fd)
            finally:
                os.close(fd)
            self._verify(name, expect)
            src, kw = self._at(tmp)
            dst, _ = self._at(name)
            fds = {"src_dir_fd": kw["dir_fd"], "dst_dir_fd": kw["dir_fd"]} if kw else {}
            os.replace(src, dst, **fds)
        except BaseException:
            self._discard(tmp)
            raise
        self._sync_dir()
        return Fingerprint(*fsprim.digest([data]))

    def remove(self, name: str, expect: Expect) -> None:
        _check_expect(expect)
        if self._verify(name, expect) is None:
            return
        target, kw = self._at(name)
        os.unlink(target, **kw)
        self._sync_dir()

    def sweep_temp(self) -> int:
        """Remove the temporary files that writes killed before their rename left behind (DESIGN §7.1):
        regular files named like TEMP_NAME, last modified more than TEMP_MIN_AGE_NS ago. Links and
        directories are skipped and remove() refuses hard-linked files; a removal that fails is left
        for the next run. Returns how many were removed."""
        removed = 0
        now = time.time_ns()
        for name in filter(TEMP_NAME.match, self.names()):
            with contextlib.suppress(OSError, SafeIOError):
                kind, _, mtime_ns = self.entry_stat(name)
                if kind == "file" and now - mtime_ns > TEMP_MIN_AGE_NS:
                    self.remove(name, ANY)
                    removed += 1
        return removed

    def _discard(self, name: str) -> None:
        with contextlib.suppress(OSError, SafeIOError, ValueError):
            target, kw = self._at(name)
            os.unlink(target, **kw)

    def _sync_dir(self) -> None:
        if os.name != "posix":
            return
        fd = self._fd if self._fd is not None else os.open(self.path, os.O_RDONLY)
        try:
            os.fsync(fd)
        except OSError as e:
            if e.errno not in fsprim.NO_DIR_SYNC:
                raise
        finally:
            if fd != self._fd:
                os.close(fd)

    def subdir(self, name: str, create: bool) -> "StoreDir":
        """Open a direct subdirectory (created 0700 when asked); links are refused."""
        found = self.kind(name)
        if found == "missing" and create:
            target, kw = self._at(name)
            os.mkdir(target, 0o700, **kw)
            found = self.kind(name)
        if found == "missing":
            raise FileNotFoundError(errno.ENOENT, "no such directory", name)
        if found != "dir":
            raise UnsafeEntry(f"{name} is not a directory ({found})")
        child = StoreDir(os.path.join(self.path, name), force_path_mode=self._fd is None)
        st = self._lstat(name)
        if child._ident != (st.st_dev, st.st_ino):
            child.close()
            raise RootChanged(f"{name} changed while it was being opened")
        return child

    @contextlib.contextmanager
    def lock(self, timeout_s: float) -> Iterator[None]:
        """Hold the OS advisory lock on .index.lock; the kernel drops it if the process dies."""
        if self.kind(LOCK_NAME) not in ("file", "missing"):
            raise UnsafeEntry(f"{LOCK_NAME} is not a regular file")
        fd = self._open(LOCK_NAME, os.O_RDWR | os.O_CREAT | NONBLOCK, 0o600)
        try:
            if kind_of(os.fstat(fd)) != "file":
                raise UnsafeEntry(f"{LOCK_NAME} is not a regular file")
            if not fsprim.acquire(fd, timeout_s):
                raise LockTimeout(f"another instance holds the lock on {self.path}")
            try:
                yield
            finally:
                fsprim.unlock(fd)
        finally:
            os.close(fd)
