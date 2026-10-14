"""Platform primitives under safeio: open flags, entry kinds, streaming reads, advisory locks."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
import time
from typing import Iterable, Iterator

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

CHUNK = 1 << 20
NO_LIMIT = sys.maxsize
POLL_S = 0.05
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
BINARY = getattr(os, "O_BINARY", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
NAME_SURROGATE = 0x20000000  # IsReparseTagNameSurrogate bit
LINK_ERRNOS = frozenset({errno.ELOOP, errno.EMLINK})
NO_DIR_SYNC = frozenset(
    {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL), getattr(errno, "EOPNOTSUPP", errno.EINVAL)}
)
_BUSY_ERRNOS = frozenset(
    {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, getattr(errno, "EDEADLOCK", errno.EAGAIN)}
)
# os.replace accepts src_dir_fd/dst_dir_fd but CPython never lists it in os.supports_dir_fd;
# it uses the same renameat() call as os.rename, so os.rename stands in for it here.
DIR_FD = (
    os.name == "posix"
    and DIRECTORY != 0
    and {os.open, os.stat, os.rename, os.unlink, os.mkdir} <= os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.listdir in os.supports_fd
)


def kind_of(st: os.stat_result) -> str:
    """'link' for symlinks and name-surrogate reparse points (junctions), else file/dir/other.

    Only name surrogates redirect a path elsewhere. Other reparse points, such as cloud-sync
    placeholders, are ordinary files to this tool, or a synced store could not be used at all.
    """
    attrs = getattr(st, "st_file_attributes", 0)
    tag = getattr(st, "st_reparse_tag", 0)
    if stat.S_ISLNK(st.st_mode) or (attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT and tag & NAME_SURROGATE):
        return "link"
    if stat.S_ISREG(st.st_mode):
        return "file"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    return "other"


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def stream(fd: int, chunk: int, limit: int) -> Iterator[bytes]:
    """Yield pieces of at most chunk bytes until EOF, or until limit + 1 bytes were read so the
    caller can tell the file was too large without reading the rest of it."""
    if chunk < 1 or limit < 0:
        raise ValueError("chunk must be positive and limit must not be negative")
    size = 0
    while size <= limit:
        piece = os.read(fd, min(chunk, limit + 1 - size))
        if not piece:
            return
        size += len(piece)
        yield piece


def digest(pieces: Iterable[bytes]) -> tuple[int, str]:
    """Total size and SHA-256 hex of a stream of pieces."""
    h = hashlib.sha256()
    size = 0
    for piece in pieces:
        h.update(piece)
        size += len(piece)
    return size, h.hexdigest()


def try_lock(fd: int) -> bool:
    """Take an exclusive advisory lock without blocking; False if another process holds it."""
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in _BUSY_ERRNOS:
            return False
        raise
    return True


def acquire(fd: int, timeout_s: float) -> bool:
    """Retry try_lock every POLL_S seconds; False if the lock is still busy after timeout_s."""
    deadline = time.monotonic() + timeout_s
    while not try_lock(fd):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)
    return True


def unlock(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
