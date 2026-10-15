"""The plugin as Claude Code runs it: hooks/hooks.json and hooks/run_hook.py in a child process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Union

from storekit import built_store, claude_adds_memory

from cc_memory_index import hook
from cc_memory_index.frontmatter import parse
from conftest import ROOT

RUN_HOOK = os.path.join(ROOT, "hooks", "run_hook.py")


def _run_hook(tmp_path: Path, payload: bytes, stdout: Union[int, None],
              script: str = RUN_HOOK) -> subprocess.CompletedProcess[bytes]:
    env = {k: v for k, v in os.environ.items() if k not in (hook.ENV_DISABLE, "PYTHONPATH")}
    env.update(HOME=str(tmp_path / "home"), USERPROFILE=str(tmp_path / "home"), CLAUDE_CONFIG_DIR=str(tmp_path / "cfg"))
    return subprocess.run([sys.executable, "-B", script], input=payload, stdout=stdout, stderr=subprocess.PIPE,
                          cwd=str(tmp_path), env=env, timeout=60)


def _edit(tmp_path: Path) -> tuple[Path, bytes]:
    """A built store in which Claude has just added a memory and its MEMORY.md line, and the payload."""
    root = built_store(tmp_path)
    claude_adds_memory(root)
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(root / "MEMORY.md")}, "session_id": "s"}
    return root, json.dumps(payload).encode("utf-8")


def _rescued(root: Path) -> bool:
    return parse((root / "new_note.md").read_text(encoding="utf-8")).get("idx") is not None


def test_run_hook_prints_one_json_object(tmp_path: Path) -> None:
    root, payload = _edit(tmp_path)
    done = _run_hook(tmp_path, payload, subprocess.PIPE)
    assert done.returncode == 0 and done.stderr == b""
    lines = done.stdout.decode("utf-8").splitlines()
    assert len(lines) == 1
    output = json.loads(lines[0])["hookSpecificOutput"]
    assert output["hookEventName"] == "PostToolUse" and "new_note.md" in output["additionalContext"]
    assert _rescued(root)


def test_run_hook_exits_0_when_stdout_is_closed(tmp_path: Path) -> None:
    root, payload = _edit(tmp_path)
    read, write = os.pipe()
    os.close(read)
    try:
        done = _run_hook(tmp_path, payload, write)
    finally:
        os.close(write)
    assert done.returncode == 0 and done.stderr == b""
    assert _rescued(root)


def test_run_hook_exits_0_when_the_package_cannot_be_imported(tmp_path: Path) -> None:
    checkout = tmp_path / "plugin"
    (checkout / "hooks").mkdir(parents=True)
    (checkout / "cc_memory_index").mkdir()
    (checkout / "cc_memory_index" / "__init__.py").write_text("raise ImportError('broken checkout')\n", encoding="utf-8")
    script = checkout / "hooks" / "run_hook.py"
    script.write_bytes(Path(RUN_HOOK).read_bytes())
    done = _run_hook(tmp_path, b"{}", subprocess.PIPE, str(script))
    assert done.returncode == 0 and done.stdout == b""
    assert done.stderr.decode("utf-8") == "cc-memory-index hook: cannot load the package: ImportError: broken checkout\n"


def test_hooks_json_runs_run_hook_for_the_tools_the_hook_accepts() -> None:
    with open(os.path.join(ROOT, "hooks", "hooks.json"), encoding="utf-8") as f:
        config = json.load(f)
    assert set(config) == {"description", "hooks"} and set(config["hooks"]) == {"PostToolUse"}
    (entry,) = config["hooks"]["PostToolUse"]
    assert set(entry["matcher"].split("|")) == hook.TOOLS
    (command,) = entry["hooks"]
    assert command["type"] == "command" and isinstance(command["timeout"], int) and command["timeout"] > 0
    assert command["command"] == 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/run_hook.py"'
    assert os.path.isfile(RUN_HOOK)
