from __future__ import annotations

import multiprocessing
import os
import signal
import stat
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from cc_memory_index.safeio import LOCK_NAME, LockTimeout, StoreDir, UnsafeEntry

WAIT_S = 30


def _hold(path: str, conn: Any) -> None:
    """Child process: hold the store lock until told to release, the pipe closes, or it is killed.

    A Pipe is used rather than an Event: killing a process that waits on a multiprocessing
    Event leaves the Event's condition variable unusable for the parent.
    """
    with StoreDir(path) as sd, sd.lock(5):
        conn.send("locked")
        try:
            conn.recv()
        except EOFError:
            pass


@pytest.fixture
def holder(tmp_path: Path) -> Iterator[tuple[Any, Callable[[], None]]]:
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    proc = ctx.Process(target=_hold, args=(str(tmp_path), child))
    proc.start()
    child.close()

    def release() -> None:
        try:
            parent.send("release")
        except OSError:
            pass

    try:
        assert parent.poll(WAIT_S) and parent.recv() == "locked", "lock holder did not start"
        yield proc, release
    finally:
        release()
        proc.join(WAIT_S)
        if proc.is_alive():
            proc.kill()
        parent.close()


def _try(path: Path, timeout_s: float) -> None:
    with StoreDir(str(path)) as sd, sd.lock(timeout_s):
        pass


def test_lock_contention_between_processes(tmp_path: Path, holder: tuple[Any, Callable[[], None]]) -> None:
    proc, release = holder
    start = time.monotonic()
    with pytest.raises(LockTimeout):
        _try(tmp_path, 0.3)
    assert time.monotonic() - start >= 0.25
    release()
    proc.join(WAIT_S)
    assert proc.exitcode == 0
    _try(tmp_path, 1)


def test_lock_is_released_when_the_holder_is_killed(
    tmp_path: Path, holder: tuple[Any, Callable[[], None]]
) -> None:
    proc, _ = holder
    with pytest.raises(LockTimeout):
        _try(tmp_path, 0.1)
    proc.kill()
    proc.join(WAIT_S)
    if os.name == "posix":
        assert proc.exitcode == -signal.SIGKILL
    _try(tmp_path, 2)


def test_lock_contention_within_one_process(tmp_path: Path) -> None:
    with StoreDir(str(tmp_path)) as a, StoreDir(str(tmp_path)) as b:
        with a.lock(1):
            with pytest.raises(LockTimeout):
                with b.lock(0.1):
                    pass
        with b.lock(1):
            pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_lock_file_is_created_0600(tmp_path: Path) -> None:
    _try(tmp_path, 1)
    assert stat.S_IMODE(os.stat(tmp_path / LOCK_NAME).st_mode) == 0o600


def test_lock_file_that_is_a_link_is_refused(tmp_path: Path, symlink: Callable[[Path, Path], None]) -> None:
    store, outside = tmp_path / "store", tmp_path / "outside"
    store.mkdir()
    symlink(store / LOCK_NAME, outside)
    with pytest.raises(UnsafeEntry):
        _try(store, 1)
    assert not outside.exists()
