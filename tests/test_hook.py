from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Optional

import pytest

from cc_memory_index import hook
from cc_memory_index.build import BuildResult


def _payload(file_path: Any, tool: str = "Write") -> bytes:
    return json.dumps({"tool_name": tool, "tool_input": {"file_path": file_path}}).encode()


def _store(tmp_path: Path, marked: bool = True) -> Path:
    store = tmp_path / "memory"
    store.mkdir(parents=True)
    if marked:
        (store / ".index-render.json").write_text("{}")
    return store


def _run(raw: bytes, env: Optional[dict[str, str]] = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = hook.main(io.BytesIO(raw), out, err, env or {})
    return code, out.getvalue(), err.getvalue()


def test_target_store_accepts_only_memory_edits_in_marked_stores(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert hook.target_store(_payload(str(store / "a.md"))) == str(store)
    assert hook.target_store(_payload(str(store / "a.md"), "MultiEdit")) == str(store)
    unmarked = _store(tmp_path / "other", marked=False)
    rejected = [
        _payload(str(unmarked / "a.md")),
        _payload(str(store / "a.txt")),
        _payload(str(store / "a.md"), "Bash"),
        _payload(None),
        _payload(42),
        json.dumps({"tool_name": "Write", "tool_input": "x"}).encode(),
        json.dumps(["Write"]).encode(),
        b"\xff\xfe not json",
        b"{",
        b"",
    ]
    for raw in rejected:
        assert hook.target_store(raw) is None, raw


def test_oversized_input_is_ignored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    raw = _payload(str(store / "a.md"))
    padded = raw[:-1] + b', "pad": "' + b"x" * hook.MAX_INPUT_BYTES + b'"}'
    assert hook.target_store(padded) is None


def test_main_is_silent_for_calls_that_are_not_ours(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called = []
    monkeypatch.setattr(hook, "run_build", lambda *a, **k: called.append(a))
    assert _run(_payload(str(tmp_path / "x.md"))) == (0, "", "")
    assert not called


def test_main_reports_through_additional_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    result = BuildResult(0, [], {"rescued": [{"file": "new_note.md", "idx": "x"}]})
    monkeypatch.setattr(hook, "run_build", lambda path, force: result)
    code, out, err = _run(_payload(str(store / "MEMORY.md")))
    assert code == 0 and err == ""
    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "new_note.md" in context and "metadata.idx" in context


def test_main_never_fails_the_tool_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)

    def boom(path: str, force: bool) -> BuildResult:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(hook, "run_build", boom)
    code, out, err = _run(_payload(str(store / "a.md")))
    assert code == 0 and out == ""
    assert err.startswith("cc-memory-index hook: RuntimeError")


def test_disable_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(hook, "run_build", lambda *a, **k: pytest.fail("must not build"))
    assert _run(_payload(str(store / "a.md")), {hook.ENV_DISABLE: "1"}) == (0, "", "")


@pytest.mark.parametrize(
    "result, expected",
    [
        (BuildResult(0, [], {}), None),
        (BuildResult(5, ["busy"], {}), None),
        (BuildResult(1, [], {"overflow": 3}), "3 resident entries"),
        (BuildResult(3, [], {"unrescuable": [{"segment": "s", "reason": "two memory links"}]}), "two memory links"),
        (BuildResult(4, [], {}), "changed while the index was rebuilt"),
        (BuildResult(6, ["permission denied"], {}), "permission denied"),
        (BuildResult(0, [], {"manual_added": ["a", "b"]}), "2 line(s)"),
    ],
)
def test_describe(result: BuildResult, expected: Optional[str]) -> None:
    text = hook.describe(result)
    if expected is None:
        assert text is None
    else:
        assert text is not None and expected in text and text.startswith("cc-memory-index: ")


def test_describe_is_bounded() -> None:
    result = BuildResult(0, [], {"rescued": [{"file": "x" * 5000 + ".md"}]})
    text = hook.describe(result)
    assert text is not None and len(text) <= len("cc-memory-index: ") + hook.MAX_CONTEXT_CHARS
