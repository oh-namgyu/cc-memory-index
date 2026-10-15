"""DESIGN §7.3-§7.4 with real processes: builds serialise on the lock, lines that another process
appends to MEMORY.md meanwhile are all rescued, and a build killed while it holds the lock leaves
nothing that delays the next one.

The appender takes no lock, like Claude Code's tools. One gap stays open by design (§7.3): an
append that lands between a build's final check of MEMORY.md and its rename is overwritten. Each
builder records those windows on a clock that processes share (see _now); a line may be missing
only if its append overlapped one of them, and any other loss fails the test.

Mutual exclusion is checked without any clock: inside the lock each builder creates a marker file
with O_EXCL, so a second holder would fail to create it.
"""

from __future__ import annotations

import collections
import contextlib
import json
import multiprocessing
import os
import random
import signal
import time
from pathlib import Path
from typing import Any, Iterator

import pytest
from faultkit import copy_store, temp_files, tree
from storekit import built_store, claude_adds_memory, memory_text

from cc_memory_index.build import run_build, run_check
from cc_memory_index.frontmatter import parse
from cc_memory_index.items import MANUAL_NAME, PENDING_NAME
from cc_memory_index.safeio import LockTimeout, StoreDir

L0, L1 = "MEMORY.md", "index-l1.md"
WAIT_S = 60
RUNS = 50
MIN_LINES, MAX_LINES = 30, 300
# 1: the appended manual lines are resident and can overflow L0's budget, a valid outcome.
# On Windows a file another process holds open cannot be replaced: such a build exits 6 (rerun).
ALLOWED = {0, 1, 4, 5, 6} if os.name == "nt" else {0, 1, 4, 5}
HELD_MARK = "held-by-a-builder"


def _now() -> int:
    """Nanoseconds on a clock every process shares. time.monotonic_ns cannot be used: on macOS,
    Python before 3.10 counts it from each process's own start."""
    if hasattr(time, "clock_gettime_ns") and hasattr(time, "CLOCK_MONOTONIC"):
        return time.clock_gettime_ns(time.CLOCK_MONOTONIC)
    return time.perf_counter_ns()  # Windows: QueryPerformanceCounter, system-wide and fine-grained


def _record_windows(windows: list[list[int]], held: list[list[int]]) -> None:
    """In a builder: log every [final check of MEMORY.md, its rename] and every lock hold."""
    started = [0]
    real_verify, real_replace, real_lock = StoreDir._verify, os.replace, StoreDir.lock

    def verify(sd: StoreDir, name: str, expect: Any) -> Any:
        if name == L0:
            started[0] = _now()
        return real_verify(sd, name, expect)

    def rename(src: Any, dst: Any, **kwargs: Any) -> None:
        real_replace(src, dst, **kwargs)
        if os.path.basename(os.fspath(dst)) == L0:
            windows.append([started[0], _now()])

    @contextlib.contextmanager
    def lock(sd: StoreDir, timeout_s: float) -> Iterator[None]:
        with real_lock(sd, timeout_s):
            start = _now()
            mark = os.path.join(os.path.dirname(sd.path), HELD_MARK)
            try:
                os.close(os.open(mark, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            except FileExistsError:
                held.append([-1, -1])  # another builder holds the lock right now
                raise
            try:
                yield
            finally:
                os.unlink(mark)
                held.append([start, _now()])

    StoreDir._verify, os.replace, StoreDir.lock = verify, rename, lock  # type: ignore[method-assign]


def _builder(path: str, seed: int, out: str) -> None:
    rng = random.Random(seed)
    windows: list[list[int]] = []
    held: list[list[int]] = []
    _record_windows(windows, held)
    codes, rescued = [], 0
    for _ in range(RUNS):
        res = run_build(path, False)
        codes.append(res.code)
        rescued += len(res.stats.get("rescued", [])) + len(res.stats.get("manual_added", []))
        time.sleep(rng.uniform(0, 0.003))
    Path(out).write_text(json.dumps({"codes": codes, "rescued": rescued, "windows": windows, "held": held}), encoding="utf-8")


def _append(path: Path, data: bytes) -> list[int]:
    """Append data in one write and return [start, end] of the append on the shared clock (_now). On
    Windows a file that is being replaced cannot be opened for a moment: try again."""
    for _ in range(1000):
        start = _now()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
        except PermissionError:
            time.sleep(0.001)
            continue
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return [start, _now()]
    raise TimeoutError(f"{path} could not be opened for appending")


def _appender(path: str, seed: int, staging: str, stop: str, out: str) -> None:
    """Add memories and append their lines (and lines without a link) to MEMORY.md, unlocked."""
    rng = random.Random(seed)
    root, lines, spans = Path(path), [], []
    for i in range(MAX_LINES):
        if i >= MIN_LINES and os.path.exists(stop):
            break
        line = f"appended rule {i}"
        if i % 2 == 0:
            name = f"c{i:03d}.md"
            draft = Path(staging) / name
            draft.write_text(memory_text(name=f"C{i}", description=f"concurrent {i}", type="project"), encoding="utf-8")
            os.replace(draft, root / name)
            line = f"[C{i}]({name}) — appended {i}"
        spans.append(_append(root / L0, f"- {line}\n".encode()))
        lines.append(line)
        time.sleep(rng.uniform(0.002, 0.008))
    Path(out).write_text(json.dumps({"lines": lines, "spans": spans}), encoding="utf-8")


def _kept(root: Path) -> set[str]:
    """Every memory's idx and every manual line."""
    kept = {parse(p.read_text(encoding="utf-8")).get("idx") or "" for p in root.glob("*.md")}
    if (root / MANUAL_NAME).exists():
        kept |= {line["seg"] for line in json.loads((root / MANUAL_NAME).read_text(encoding="utf-8"))["lines"]}
    return kept


def _overlaps(span: list[int], windows: list[list[int]]) -> bool:
    return any(span[0] <= end and start <= span[1] for start, end in windows)


def _run_all(tmp_path: Path, root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ctx = multiprocessing.get_context("spawn")
    (tmp_path / "staging").mkdir()
    outs = [tmp_path / f"builder{n}.json" for n in (1, 2)]
    stop, appended = tmp_path / "stop", tmp_path / "appender.json"
    builders = [ctx.Process(target=_builder, args=(str(root), n, str(out))) for n, out in enumerate(outs, 1)]
    appender = ctx.Process(target=_appender, args=(str(root), 3, str(tmp_path / "staging"), str(stop), str(appended)))
    for proc in [appender, *builders]:
        proc.start()
    try:
        for proc in builders:
            proc.join(WAIT_S)
        stop.touch()
        appender.join(WAIT_S)
        assert [proc.exitcode for proc in [*builders, appender]] == [0, 0, 0]
    finally:
        for proc in [appender, *builders]:
            if proc.is_alive():
                proc.kill()
    return [json.loads(out.read_text(encoding="utf-8")) for out in outs], json.loads(appended.read_text(encoding="utf-8"))


def test_two_builders_and_an_unlocked_appender_lose_no_line(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    builders, appender = _run_all(tmp_path, root)
    first, second = builders[0]["held"], builders[1]["held"]
    assert first and second and [-1, -1] not in first + second, "the lock was held twice"
    codes = collections.Counter(code for b in builders for code in b["codes"])
    assert set(codes) <= ALLOWED and sum(codes.values()) == 2 * RUNS, codes
    assert sum(b["rescued"] for b in builders) > 0 and len(appender["lines"]) >= MIN_LINES
    final = run_build(str(root), False)
    assert final.code in (0, 1) and run_check(str(root)).code == 0, final.messages
    assert json.loads((root / PENDING_NAME).read_text(encoding="utf-8"))["entries"] == []
    windows = [w for b in builders for w in b["windows"]]
    kept = _kept(root)
    lost = [(line, span) for line, span in zip(appender["lines"], appender["spans"]) if line not in kept]
    assert [line for line, span in lost if not _overlaps(span, windows)] == []
    assert len(lost) <= len(appender["lines"]) // 10, f"{len(lost)} lines fell into the documented window"


def _stall_before_l1(path: str, conn: Any) -> None:
    """A build that stops for good right before it replaces L1, holding the lock."""
    real = StoreDir.replace

    def replace(sd: StoreDir, name: str, data: bytes, expect: Any) -> Any:
        if name == L1:
            conn.send("stalled")
            time.sleep(WAIT_S)
        return real(sd, name, data, expect)

    StoreDir.replace = replace  # type: ignore[method-assign]
    run_build(path, False)


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL and its exit status are POSIX")
def test_a_build_killed_holding_the_lock_does_not_delay_the_next(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    claude_adds_memory(root)
    clean = copy_store(root, tmp_path / "clean")
    assert run_build(str(clean), False).code == 0
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    proc = ctx.Process(target=_stall_before_l1, args=(str(root), child))
    proc.start()
    try:
        assert parent.poll(WAIT_S) and parent.recv() == "stalled"
        with StoreDir(str(root)) as sd, pytest.raises(LockTimeout):
            with sd.lock(0.2):
                pass
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(WAIT_S)
        assert proc.exitcode == -signal.SIGKILL
    finally:
        if proc.is_alive():
            proc.kill()
    assert json.loads((root / PENDING_NAME).read_text(encoding="utf-8"))["entries"] == []
    start = time.monotonic()
    res = run_build(str(root), False)
    assert res.code == 0 and time.monotonic() - start < 1.0, res.messages
    assert tree(root) == tree(clean) and temp_files(root) == []
