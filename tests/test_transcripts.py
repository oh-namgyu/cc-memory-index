from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

import pytest
from storekit import project_store, write_transcript

from cc_memory_index import transcripts as tr
from cc_memory_index.safeio import StoreDir
from cc_memory_index.transcripts import Mentions, count_mentions, newest, transcripts_dir

Symlink = Callable[[Path, Path], None]
SECRET = "sk-ant-api03-TOPSECRETVALUE-do-not-leak"
NAMES = {"alpha.md", "beta.md", "foo.md", "foo.md.bak.md", "gamma.md"}


def _line(path: str) -> str:
    return '{"type":"tool_use","input":{"file_path":"' + path + '","content":"' + SECRET + '"}}\n'


def _counts(root: Path, n: int = 10, names: set[str] = NAMES) -> tuple[dict[str, int], list[str]]:
    notes: list[str] = []
    return count_mentions(str(root), names, n, notes), notes


def test_counts_each_name_once_per_transcript(tmp_path: Path) -> None:
    root = project_store(tmp_path)
    write_transcript(root.parent, "t1.jsonl", _line("/p/memory/alpha.md") * 3 + _line("/p/memory/beta.md"), 1)
    write_transcript(root.parent, "t2.jsonl", _line("/p/memory/alpha.md") + _line("/p/memory/other.md"), 2)
    counts, notes = _counts(root)
    assert counts == {"alpha.md": 2, "beta.md": 1, "foo.md": 0, "foo.md.bak.md": 0, "gamma.md": 0}
    assert notes == ["transcripts: counted mentions in 2 of the 2 most recent (up to 10 asked for)"]


def test_only_the_n_most_recent_are_read(tmp_path: Path) -> None:
    root = project_store(tmp_path)
    for age, name in enumerate(["alpha.md", "beta.md", "gamma.md"]):
        write_transcript(root.parent, f"s{age}.jsonl", _line(f"/p/memory/{name}"), age)
    counts, _ = _counts(root, n=2)
    assert (counts["alpha.md"], counts["beta.md"], counts["gamma.md"]) == (1, 1, 0)
    with StoreDir(str(root.parent)) as sd:
        assert [name for name, _ in newest(sd, 50)] == ["s0.jsonl", "s1.jsonl", "s2.jsonl"]


def test_windows_separators_plain_and_json_escaped(tmp_path: Path) -> None:
    root = project_store(tmp_path)
    write_transcript(root.parent, "w.jsonl", '"C:\\\\p\\\\memory\\\\alpha.md" memory\\beta.md', 0)
    counts, _ = _counts(root)
    assert (counts["alpha.md"], counts["beta.md"]) == (1, 1)


TEXT = (
    "x" * 37 + "memory/alpha.md, then memory/foo.md.bak.md and memory\\\\beta.md;"
    + "y" * 23 + "memory/alpha.md" + "z" * 5 + " memory/gamma.md"
)
EXPECTED = {"alpha.md": 1, "beta.md": 1, "foo.md": 0, "foo.md.bak.md": 1, "gamma.md": 1}


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 11, 16, 29, 64, 4096])
def test_mentions_across_chunk_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chunk: int) -> None:
    root = project_store(tmp_path)
    write_transcript(root.parent, "b.jsonl", TEXT, 0)
    monkeypatch.setattr(tr, "CHUNK_BYTES", chunk)
    assert _counts(root)[0] == EXPECTED


def test_every_boundary_position_matches_a_whole_scan() -> None:
    data = TEXT.encode()
    whole = Mentions(NAMES)
    whole.feed(data)
    expected = whole.finish()
    for cut in range(len(data) + 1):
        split = Mentions(NAMES)
        split.feed(data[:cut])
        split.feed(data[cut:])
        assert split.finish() == expected, cut
    assert expected == {name for name, n in EXPECTED.items() if n}


def test_links_and_directories_are_never_read(tmp_path: Path, symlink: Symlink) -> None:
    root = project_store(tmp_path)
    outside = write_transcript(tmp_path, "outside.jsonl", _line("/p/memory/alpha.md"), 0)
    symlink(root.parent / "z.jsonl", outside)
    (root.parent / "d.jsonl").mkdir()
    write_transcript(root.parent, "real.jsonl", _line("/p/memory/beta.md"), 5)
    counts, notes = _counts(root)
    assert (counts["alpha.md"], counts["beta.md"]) == (0, 1)
    assert "1 of the 1 most recent" in notes[-1]


def test_transcripts_live_next_to_the_unresolved_store(tmp_path: Path, symlink: Symlink) -> None:
    synced = tmp_path / "synced" / "memory"
    synced.mkdir(parents=True)
    write_transcript(synced.parent, "wrong.jsonl", _line("/p/memory/alpha.md"), 0)
    project = tmp_path / "project"
    project.mkdir()
    link = project / "memory"
    symlink(link, synced)
    write_transcript(project, "right.jsonl", _line("/p/memory/beta.md"), 0)
    assert transcripts_dir(str(link) + os.sep) == str(project)
    counts, _ = _counts(link)
    assert (counts["alpha.md"], counts["beta.md"]) == (0, 1)


def _assert_private(notes: list[str]) -> None:
    text = "\n".join(notes)
    assert SECRET not in text and "TOPSECRET" not in text and "tool_use" not in text


def test_a_transcript_over_the_file_cap_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = project_store(tmp_path)
    write_transcript(root.parent, "big.jsonl", _line("/p/memory/alpha.md") * 4, 0)
    write_transcript(root.parent, "small.jsonl", _line("/p/memory/beta.md"), 1)
    monkeypatch.setattr(tr, "FILE_MAX_BYTES", 200)
    counts, notes = _counts(root)
    assert (counts["alpha.md"], counts["beta.md"]) == (0, 1)
    assert notes[0] == "transcript 'big.jsonl' skipped: larger than 0 MiB"
    _assert_private(notes)


def test_a_transcript_that_grows_while_read_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = project_store(tmp_path)
    write_transcript(root.parent, "grew.jsonl", _line("/p/memory/alpha.md") * 4, 0)
    monkeypatch.setattr(tr, "FILE_MAX_BYTES", 200)
    monkeypatch.setattr(tr, "CHUNK_BYTES", 64)
    monkeypatch.setattr(tr, "newest", lambda sd, n: [("grew.jsonl", 10)])
    counts, notes = _counts(root)
    assert counts["alpha.md"] == 0
    assert notes[0] == "transcript 'grew.jsonl' skipped: it grew past the read limit while being read"
    _assert_private(notes)


def test_reading_stops_at_the_total_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = project_store(tmp_path)
    for age, name in enumerate(["alpha.md", "beta.md", "gamma.md"]):
        write_transcript(root.parent, f"s{age}.jsonl", _line(f"/p/memory/{name}"), age)
    size = (root.parent / "s0.jsonl").stat().st_size
    monkeypatch.setattr(tr, "TOTAL_MAX_BYTES", 2 * size + size // 2)
    counts, notes = _counts(root)
    assert (counts["alpha.md"], counts["beta.md"], counts["gamma.md"]) == (1, 1, 0)
    assert re.fullmatch(r"transcripts: stopped before 's2\.jsonl': 0 MiB read in total", notes[0])
    _assert_private(notes)


def test_only_the_names_asked_for_are_returned(tmp_path: Path) -> None:
    root = project_store(tmp_path)
    write_transcript(root.parent, "t.jsonl", _line("/p/memory/alpha.md") + _line("/p/memory/secret-name.md"), 0)
    counts, _ = _counts(root, names={"alpha.md", "zeta.md"})
    assert counts == {"alpha.md": 1, "zeta.md": 0}


def test_no_transcripts_is_not_an_error(tmp_path: Path) -> None:
    root = project_store(tmp_path)
    counts, notes = _counts(root, names={"alpha.md"})
    assert counts == {"alpha.md": 0} and "0 of the 0 most recent" in notes[-1]


@pytest.mark.parametrize("n", [0, 51, -1, True])
def test_number_of_transcripts_is_bounded(tmp_path: Path, n: int) -> None:
    root = project_store(tmp_path)
    with pytest.raises(ValueError):
        count_mentions(str(root), {"a.md"}, n)
