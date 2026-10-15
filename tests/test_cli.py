from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from storekit import built_store, make_store, sample_store, write_index, write_memory

from cc_memory_index import cli
from cc_memory_index.migrate_snapshot import SNAPSHOT_PREFIX
from cc_memory_index.transcripts import MAX_TRANSCRIPTS
from conftest import ROOT


def _json(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict[str, object], str]:
    code = cli.main(argv)
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert len(lines) == 1, out
    return code, json.loads(lines[0]), err


def test_build_json_shape_and_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = sample_store(tmp_path)
    code, obj, err = _json(capsys, ["build", "--dir", str(root), "--json"])
    assert code == 0 and err == ""
    assert set(obj) == {"command", "code", "messages", "stats"}
    assert (obj["command"], obj["code"], obj["messages"]) == ("build", 0, [])
    stats = obj["stats"]
    assert isinstance(stats, dict) and stats["written"] == ["index-l1.md", "MEMORY.md", ".index-render.json"]


def test_options_before_the_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = built_store(tmp_path)
    code, obj, _ = _json(capsys, ["--json", "--dir", str(root), "check"])
    assert code == 0 and obj["command"] == "check" and obj["code"] == 0


def test_build_runs_are_summarised(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = sample_store(tmp_path)
    assert cli.main(["build", "--dir", str(root)]) == 0
    out = capsys.readouterr().out
    assert "build: 3 memory files; 2 of 2 resident entries in MEMORY.md, 1 in index-l1.md" in out
    assert cli.main(["build", "--dir", str(root)]) == 0
    assert "up to date: nothing written" in capsys.readouterr().out
    assert cli.main(["check", "--dir", str(root)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "in sync"


def test_force_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = built_store(tmp_path)
    (root / "MEMORY.md").write_text("- by hand\n", encoding="utf-8")
    assert cli.main(["build", "--force", "--dir", str(root)]) == 0
    out, err = capsys.readouterr()
    (backup,) = [name for name in os.listdir(root) if name.startswith(".MEMORY.md.bak-")]
    assert backup in out and err == ""


def test_store_from_environment(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                monkeypatch: pytest.MonkeyPatch) -> None:
    root = sample_store(tmp_path)
    monkeypatch.setenv("CC_MEMORY_INDEX_DIR", str(root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["build"]) == 0 and (root / "MEMORY.md").exists()
    capsys.readouterr()


@pytest.mark.parametrize("argv", [
    [], ["nope"], ["check", "--force"], ["suggest", "--transcripts", "0"], ["suggest", "--transcripts", "x"],
    ["suggest", "--transcripts", str(MAX_TRANSCRIPTS + 1)], ["migrate", "--apply", "--restore", "x"],
    ["hook", "--dir", "x"], ["--dir", "x", "hook"], ["--json", "hook"],
])
def test_usage_errors_exit_2(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(argv)
    assert info.value.code == 2
    out, err = capsys.readouterr()
    assert out == "" and "usage: cc-memory-index" in err


def test_transcripts_error_is_one_short_line(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["suggest", "--transcripts", "51"])
    error = capsys.readouterr().err.splitlines()[-1]
    assert error.endswith(f"argument --transcripts: expected a whole number from 1 to {MAX_TRANSCRIPTS}, not '51'")


def test_hook_takes_no_store_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--dir", "x", "hook"])
    assert "hook takes no --dir or --json" in capsys.readouterr().err


@pytest.mark.parametrize("form", ["{id}", SNAPSHOT_PREFIX + "{id}", SNAPSHOT_PREFIX + "{id}/"])
def test_restore_takes_the_snapshot_id_or_its_directory_name(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                            form: str) -> None:
    root = make_store(tmp_path)
    write_memory(root, "a.md", name="A", description="a")
    hand_written = write_index(root, "# Memory\n\n- [A](a.md) — a\n").read_bytes()
    code, obj, _ = _json(capsys, ["migrate", "--apply", "--json", "--dir", str(root)])
    stats = obj["stats"]
    assert code == 0 and isinstance(stats, dict)
    snapshot = form.format(id=stats["snapshot"][len(SNAPSHOT_PREFIX):])
    code, obj, _ = _json(capsys, ["migrate", "--restore", snapshot, "--json", "--dir", str(root)])
    assert code == 0, obj["messages"]
    assert (root / "MEMORY.md").read_bytes() == hand_written


def test_extension_point_lists_the_commands() -> None:
    assert list(cli.COMMANDS) == ["build", "check", "lint", "suggest", "demote", "migrate"]


def test_python_dash_m_help(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": ROOT}
    for argv in (["--help"], ["build", "--help"]):
        done = subprocess.run([sys.executable, "-B", "-m", "cc_memory_index", *argv], cwd=str(tmp_path), env=env,
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0 and "--dir" in done.stdout and "--json" in done.stdout
    assert "--force" in done.stdout and "MEMORY.md and the L1 file" in " ".join(done.stdout.split())
    assert os.listdir(tmp_path) == []
