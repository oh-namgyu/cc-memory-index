from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from storekit import built_store, sample_store

from cc_memory_index import cli
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


def test_json_reports_refusals(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = built_store(tmp_path)
    with (root / "MEMORY.md").open("a") as f:
        f.write("- by hand\n")
    code, obj, err = _json(capsys, ["build", "--json", "--dir", str(root)])
    assert code == obj["code"] == 3 and err == ""
    messages = obj["messages"]
    assert isinstance(messages, list) and "MEMORY.md" in messages[0]


def test_human_summary_on_stdout_warnings_on_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = sample_store(tmp_path)
    (root / "broken.md").write_text("---\nname: x\n")
    assert cli.main(["build", "--dir", str(root)]) == 7
    out, err = capsys.readouterr()
    assert "broken.md" in err and "broken.md" not in out
    assert "MEMORY.md:" in out and "wrote index-l1.md, MEMORY.md, .index-render.json" in out and "exit 7" in out
    assert cli.main(["build", "--dir", str(root)]) == 7
    assert "up to date: nothing written" in capsys.readouterr().out
    assert cli.main(["check", "--dir", str(root)]) == 7
    assert "in sync" in capsys.readouterr().out


def test_check_out_of_sync(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = sample_store(tmp_path)
    assert cli.main(["check", "--dir", str(root)]) == 1
    out, err = capsys.readouterr()
    assert "out of sync: MEMORY.md, index-l1.md" in out and err == ""


def test_force_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = built_store(tmp_path)
    (root / "MEMORY.md").write_text("- by hand\n")
    assert cli.main(["build", "--force", "--dir", str(root)]) == 0
    assert "--force" in capsys.readouterr().err


def test_store_not_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = str(tmp_path / "nope")
    assert cli.main(["check", "--dir", missing]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "no memory store" in err
    assert cli.main(["build", "--dir", missing, "--json"]) == 2
    out, err = capsys.readouterr()
    assert json.loads(out)["code"] == 2 and "no memory store" in err


def test_store_from_environment(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                monkeypatch: pytest.MonkeyPatch) -> None:
    root = sample_store(tmp_path)
    monkeypatch.setenv("CC_MEMORY_INDEX_DIR", str(root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["build"]) == 0 and (root / "MEMORY.md").exists()
    capsys.readouterr()


def test_usage_errors_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in ([], ["nope"], ["check", "--force"]):
        with pytest.raises(SystemExit) as info:
            cli.main(argv)
        assert info.value.code == 2
    capsys.readouterr()


def test_extension_point_lists_the_commands() -> None:
    assert list(cli.COMMANDS) == ["build", "check"]


def test_python_dash_m_help() -> None:
    env = {**os.environ, "PYTHONPATH": ROOT}
    for argv in (["--help"], ["build", "--help"]):
        done = subprocess.run([sys.executable, "-m", "cc_memory_index", *argv], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0 and "--dir" in done.stdout and "--json" in done.stdout
    assert "--force" in done.stdout
