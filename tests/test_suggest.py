from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from storekit import make_store, project_store, sample_store, snapshot, write_json, write_memory, write_transcript

from cc_memory_index import transcripts as tr
from cc_memory_index.build import BuildResult, run_build, run_check
from cc_memory_index.demote import demote_files
from cc_memory_index.frontmatter import parse
from cc_memory_index.items import MANUAL_NAME
from cc_memory_index.suggest import run_suggest

TIGHT = {"budget_bytes": 2048, "budget_lines": 20}
SECRET = "sk-ant-api03-TOPSECRETVALUE-do-not-leak"
NAMES = [f"m{i:02d}.md" for i in range(14)]


def fill_store(root: Path) -> Path:
    """Fourteen resident memories of different sizes that do not all fit TIGHT."""
    for i, file in enumerate(NAMES):
        idx = f"[{file[:-3]}]({file}) — " + "w" * (90 + 10 * i)
        write_memory(root, file, name=file[:-3], type="reference", tier="resident", idx=idx)
    return root


def _files(res: BuildResult) -> list[str]:
    return [c["file"] for c in res.stats["proposal"]]


def _overflow_after(tmp_path: Path, root: Path, files: list[str]) -> int:
    twin = tmp_path / f"twin{len(files)}"
    shutil.copytree(root, twin)
    assert demote_files(str(twin), files).code == 0
    return int(run_build(str(twin), False).stats["overflow"])


def test_within_budget_proposes_nothing_and_writes_nothing(tmp_path: Path) -> None:
    root = sample_store(tmp_path)
    before = snapshot(root)
    res = run_suggest(str(root), transcripts=3)
    assert res.code == 0 and res.stats["overflow"] == 0 and res.stats["proposal"] == []
    assert res.messages == ["within budget: every resident entry fits MEMORY.md; nothing to demote"]
    assert snapshot(root) == before and not (root / ".index.lock").exists()


def test_overflow_without_transcripts_takes_the_largest_first(tmp_path: Path) -> None:
    root = fill_store(make_store(tmp_path, TIGHT))
    before = snapshot(root)
    res = run_suggest(str(root))
    files = _files(res)
    assert res.code == 0 and res.stats["overflow"] > 0 and files
    assert files == sorted(NAMES, reverse=True)[: len(files)]
    sizes = [c["bytes"] for c in res.stats["proposal"]]
    assert sizes == sorted(sizes, reverse=True) and all(c["mentions"] is None for c in res.stats["proposal"])
    assert res.stats["l0_bytes_after"] <= TIGHT["budget_bytes"] and res.stats["l0_lines_after"] <= 20
    assert snapshot(root) == before
    assert _overflow_after(tmp_path, root, files) == 0
    assert _overflow_after(tmp_path, root, files[:-1]) > 0


def _transcripts(root: Path, mentioned: dict[str, int], count: int = 3) -> None:
    for t in range(count):
        lines = [f'{{"file_path":"/p/memory/{f}","note":"{SECRET}"}}\n' for f, n in mentioned.items() if n > t]
        write_transcript(root.parent, f"t{t}.jsonl", "".join(lines), t)


def test_transcripts_rank_rarely_mentioned_files_first(tmp_path: Path) -> None:
    root = fill_store(project_store(tmp_path, TIGHT))
    mentions = {f: 3 for f in NAMES}
    mentions.update({"m03.md": 0, "m05.md": 1})
    _transcripts(root, mentions)
    res = run_suggest(str(root), transcripts=3)
    proposal = res.stats["proposal"]
    assert res.code == 0 and res.stats["transcripts"] == 3
    assert [c["file"] for c in proposal][:2] == ["m03.md", "m05.md"]
    assert [c["mentions"] for c in proposal][:2] == [0, 1]
    assert "transcripts: counted mentions in 3 of the 3 most recent (up to 3 asked for)" in res.messages
    assert _overflow_after(tmp_path, root, [c["file"] for c in proposal]) == 0


def test_oversized_item_comes_first_even_when_often_mentioned(tmp_path: Path) -> None:
    root = fill_store(project_store(tmp_path, TIGHT))
    write_memory(root, "huge.md", name="huge", tier="resident", idx="[huge](huge.md) — " + "h" * 400)
    _transcripts(root, {"huge.md": 3, "m00.md": 0})
    proposal = run_suggest(str(root), transcripts=3).stats["proposal"]
    assert proposal[0]["file"] == "huge.md" and proposal[0]["oversized"] and proposal[0]["mentions"] == 3


def _assert_private(res: BuildResult, captured: Any, caplog: pytest.LogCaptureFixture) -> None:
    dumped = json.dumps({"messages": res.messages, "stats": res.stats}) + captured.out + captured.err
    dumped += "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET not in dumped and "TOPSECRET" not in dumped


def test_transcript_text_never_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    root = fill_store(project_store(tmp_path, TIGHT))
    _transcripts(root, {f: 2 for f in NAMES}, count=4)
    _assert_private(run_suggest(str(root), transcripts=4), capsys.readouterr(), caplog)
    size = (root.parent / "t0.jsonl").stat().st_size
    monkeypatch.setattr(tr, "FILE_MAX_BYTES", size - 1)
    res = run_suggest(str(root), transcripts=4)
    assert any(m.startswith("transcript 't0.jsonl' skipped") for m in res.messages)
    _assert_private(res, capsys.readouterr(), caplog)
    monkeypatch.setattr(tr, "FILE_MAX_BYTES", 1 << 20)
    monkeypatch.setattr(tr, "TOTAL_MAX_BYTES", size + 1)
    monkeypatch.setattr(tr, "CHUNK_BYTES", 7)
    res = run_suggest(str(root), transcripts=4)
    assert any(m.startswith("transcripts: stopped before") for m in res.messages)
    _assert_private(res, capsys.readouterr(), caplog)
    monkeypatch.setattr(tr, "TOTAL_MAX_BYTES", 1 << 20)
    if os.name == "posix" and os.geteuid() != 0:
        (root.parent / "t1.jsonl").chmod(0)
        res = run_suggest(str(root), transcripts=4)
        assert any(m.startswith("transcript 't1.jsonl' skipped") for m in res.messages)
        _assert_private(res, capsys.readouterr(), caplog)
    res = run_suggest(str(root), transcripts=4, apply=True)
    assert res.code == 0
    _assert_private(res, capsys.readouterr(), caplog)


def test_apply_demotes_the_proposal_and_builds_within_budget(tmp_path: Path) -> None:
    root = fill_store(make_store(tmp_path, TIGHT))
    assert run_build(str(root), False).code == 1
    proposed = _files(run_suggest(str(root)))
    res = run_suggest(str(root), apply=True)
    assert res.code == 0 and res.stats["demoted"] == proposed == _files(res)
    assert res.stats["build"]["overflow"] == 0 and "Warning" not in (root / "MEMORY.md").read_text()
    for file in NAMES:
        tier = parse((root / file).read_text()).get("tier")
        assert tier == ("indexed" if file in proposed else "resident")
    assert run_check(str(root)).code == 0
    assert run_suggest(str(root)).stats["proposal"] == []


def test_overflow_that_demotion_cannot_fix_exits_1(tmp_path: Path) -> None:
    root = make_store(tmp_path, TIGHT)
    lines = [{"seg": f"manual line {i} " + "m" * 200, "section": "Notes", "sub": "", "order": i} for i in range(12)]
    write_json(root, MANUAL_NAME, {"version": 1, "preamble": None, "lines": lines})
    write_memory(root, "a.md", name="a", tier="resident")
    before = snapshot(root)
    res = run_suggest(str(root), apply=True)
    assert res.code == 1 and "would not fix it" in res.messages[-1]
    assert snapshot(root) == before


@pytest.mark.parametrize("n", [0, 51, True])
def test_transcript_count_is_validated(tmp_path: Path, n: int) -> None:
    res = run_suggest(str(sample_store(tmp_path)), transcripts=n)
    assert res.code == 2 and "between 1 and 50" in res.messages[0]


def test_interrupted_migration_refuses(tmp_path: Path) -> None:
    root = fill_store(make_store(tmp_path, TIGHT))
    (root / ".index-migrating").write_text("")
    assert run_suggest(str(root)).code == 1
    assert run_suggest(str(root), apply=True).code == 2
