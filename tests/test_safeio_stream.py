from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Generator, Iterator, cast

import pytest

from cc_memory_index.safeio import StoreDir, TooLarge, UnsafeEntry

Symlink = Callable[[Path, Path], None]
DATA = bytes(range(256)) * 40


@pytest.fixture(params=["dir_fd", "path"])
def sd(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StoreDir]:
    root = tmp_path / "store"
    root.mkdir()
    (root / "t.jsonl").write_bytes(DATA)
    with StoreDir(str(root), force_path_mode=request.param == "path") as d:
        yield d


def test_chunks_concatenate_to_the_file(sd: StoreDir) -> None:
    pieces = list(sd.iter_chunks("t.jsonl", 1000, len(DATA)))
    assert b"".join(pieces) == DATA
    assert all(0 < len(p) <= 1000 for p in pieces) and len(pieces) == 11


def test_empty_file_yields_nothing(sd: StoreDir) -> None:
    (Path(sd.path) / "e.jsonl").write_bytes(b"")
    assert list(sd.iter_chunks("e.jsonl", 10, 0)) == []


def test_too_large_after_max_bytes_were_read(sd: StoreDir) -> None:
    got: list[bytes] = []
    with pytest.raises(TooLarge) as info:
        for piece in sd.iter_chunks("t.jsonl", 1000, len(DATA) - 1):
            got.append(piece)
    assert b"".join(got) == DATA[:10000]
    assert "t.jsonl" in str(info.value) and str(DATA[:8]) not in str(info.value)


def test_link_is_refused(sd: StoreDir, symlink: Symlink) -> None:
    symlink(Path(sd.path) / "l.jsonl", Path(sd.path) / "t.jsonl")
    with pytest.raises(UnsafeEntry):
        next(sd.iter_chunks("l.jsonl", 10, 100))


def test_directory_and_missing_entry_are_refused(sd: StoreDir) -> None:
    (Path(sd.path) / "d.jsonl").mkdir()
    with pytest.raises(UnsafeEntry):
        next(sd.iter_chunks("d.jsonl", 10, 100))
    with pytest.raises(FileNotFoundError):
        next(sd.iter_chunks("none.jsonl", 10, 100))


def test_bad_arguments_are_refused(sd: StoreDir) -> None:
    with pytest.raises(ValueError):
        next(sd.iter_chunks("t.jsonl", 0, 100))
    with pytest.raises(ValueError):
        next(sd.iter_chunks("t.jsonl", 10, -1))


def test_abandoned_stream_closes_its_descriptor(sd: StoreDir, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    real = os.close

    def record(fd: int) -> None:
        closed.append(fd)
        real(fd)

    gen = cast(Generator[bytes, None, None], sd.iter_chunks("t.jsonl", 10, len(DATA)))
    assert next(gen) == DATA[:10]
    monkeypatch.setattr(os, "close", record)
    gen.close()
    assert len(closed) == 1


def test_read_and_fingerprint_still_agree(sd: StoreDir) -> None:
    data, fp = sd.read("t.jsonl")
    assert data == DATA and sd.fingerprint("t.jsonl") == fp and sd.fingerprint("none.md") is None
    with pytest.raises(TooLarge):
        sd.read("t.jsonl", len(DATA) - 1)
