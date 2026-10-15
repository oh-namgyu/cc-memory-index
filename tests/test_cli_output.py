"""Where the command line prints (DESIGN §10): exit 0 or 1 on stdout, 2 or more on stderr, --json alone."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest
from storekit import append, built_store, make_store, memory_text, sample_store, write_index, write_json, write_memory

from cc_memory_index import cli
from cc_memory_index.build import run_build
from conftest import ROOT

Setup = Callable[[Path], list[str]]


def _store(argv: list[str], root: Path) -> list[str]:
    return [*argv, "--dir", str(root)]


def _hand_written(tmp_path: Path) -> Path:
    root = make_store(tmp_path)
    write_memory(root, "a.md", name="A", description="a")
    write_index(root, "# Memory\n\n- [A](a.md) — a\n")
    return root


def _refused(tmp_path: Path) -> list[str]:
    root = built_store(tmp_path)
    append(root, "MEMORY.md", "- [x](missing.md) — points at no memory file\n")
    return _store(["build"], root)


def _unparseable(tmp_path: Path) -> list[str]:
    root = sample_store(tmp_path)
    (root / "broken.md").write_text("---\nname: x\n", encoding="utf-8")
    return _store(["build"], root)


def _wiki(root: Path, target: str) -> None:
    (root / "wiki.md").write_bytes(memory_text(f"see [[{target}]]\n", name="wiki").encode("utf-8"))


def _lint_errors(tmp_path: Path) -> list[str]:
    root = sample_store(tmp_path)
    _wiki(root, "nowhere")
    assert run_build(str(root), False).code == 0
    return _store(["lint"], root)


def _bad_config(tmp_path: Path) -> list[str]:
    root = sample_store(tmp_path)
    write_json(root, ".memory-index.json", {"bogus": 1})
    return _store(["lint"], root)


# Expected text is data (file names, config keys) or the CLI's own summary lines, never the
# wording of a core module's message.
CASES: list[tuple[str, Setup, int, list[str]]] = [
    ("build", lambda p: _store(["build"], sample_store(p)), 0, ["wrote index-l1.md, MEMORY.md, .index-render.json"]),
    ("build", _refused, 3, ["missing.md"]),
    ("build", _unparseable, 7, ["broken.md", "wrote index-l1.md"]),
    ("check", lambda p: _store(["check"], sample_store(p)), 1, ["out of sync: MEMORY.md, index-l1.md"]),
    ("check", lambda p: ["check", "--dir", str(p / "nope")], 2, ["nope"]),
    ("lint", _lint_errors, 1, ["nowhere", "lint: 1 error(s)"]),
    ("lint", _bad_config, 2, ["bogus"]),
    ("suggest", lambda p: _store(["suggest"], built_store(p)), 0, []),
    ("demote", lambda p: _store(["demote", "tea.md"], built_store(p)), 0, ["tea.md"]),
    ("demote", lambda p: _store(["demote", "nope.md"], built_store(p)), 2, ["nope.md"]),
    ("migrate", lambda p: _store(["migrate"], _hand_written(p)), 0, []),
    ("migrate", lambda p: _store(["migrate", "--restore", "bogus"], _hand_written(p)), 2, ["bogus"]),
]


@pytest.mark.parametrize("command, setup, code, expected", CASES)
def test_report_goes_to_one_stream_chosen_by_the_exit_code(
        tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, setup: Setup, code: int,
        expected: list[str]) -> None:
    assert cli.main(setup(tmp_path)) == code
    out, err = capsys.readouterr()
    report, other = (out, err) if code <= 1 else (err, out)
    assert other == "" and report.endswith("\n")
    for text in expected:
        assert text in report
    assert report.count(f"{command}: exit") == (0 if code == 0 else 1)
    if code:
        assert report.splitlines()[-1] == f"{command}: exit {code}"


@pytest.mark.parametrize("command, setup, code, expected", CASES)
def test_json_is_the_only_output(tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, setup: Setup,
                                 code: int, expected: list[str]) -> None:
    assert cli.main([*setup(tmp_path), "--json"]) == code
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert err == "" and len(lines) == 1
    obj = json.loads(lines[0])
    assert (obj["command"], obj["code"]) == (command, code) and set(obj) == {"command", "code", "messages", "stats"}


def _child(argv: list[str], tmp_path: Path, stdout: int, encoding: str = "utf-8") -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": encoding, "HOME": str(tmp_path)}
    env.pop("CC_MEMORY_INDEX_DIR", None)
    return subprocess.run([sys.executable, "-B", "-m", "cc_memory_index", *argv], stdout=stdout,
                          stderr=subprocess.PIPE, cwd=str(tmp_path), env=env, timeout=60)


def test_text_the_console_cannot_encode_is_escaped(tmp_path: Path) -> None:
    root = built_store(tmp_path)
    _wiki(root, "메모")
    done = _child(["lint", "--dir", str(root)], tmp_path, subprocess.PIPE, encoding="cp1252")
    assert done.returncode == 1 and done.stderr == b""
    assert b"wiki link '\\uba54\\ubaa8' does not resolve" in done.stdout


@pytest.mark.parametrize("argv, code", [(["check"], 1), (["--help"], 0)])
def test_a_reader_that_goes_away_is_not_an_error(tmp_path: Path, argv: list[str], code: int) -> None:
    root = sample_store(tmp_path)
    read, write = os.pipe()
    os.close(read)
    try:
        done = _child([*argv, "--dir", str(root)], tmp_path, write)
    finally:
        os.close(write)
    assert done.returncode == code and done.stderr == b""
