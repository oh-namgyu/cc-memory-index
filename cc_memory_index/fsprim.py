"""Platform primitives under safeio: open flags, entry kinds, draining, advisory locks."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
import time
from typing import Optional

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

CHUNK = 1 << 20
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


def drain(fd: int, limit: Optional[int], keep: bool) -> tuple[bytes, int, str]:
    """Read to EOF, or up to limit + 1 bytes so the caller can tell the file was too large.

    Returns (data if keep else b"", size, sha256 hex).
    """
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    size = 0
    while limit is None or size <= limit:
        chunk = os.read(fd, CHUNK if limit is None else min(CHUNK, limit + 1 - size))
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
        if keep:
            chunks.append(chunk)
    return b"".join(chunks), size, digest.hexdigest()


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
