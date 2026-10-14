from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable, Optional, Union

import pytest

from cc_memory_index import store
from cc_memory_index.safeio import Fingerprint, StoreDir
from cc_memory_index.store import Config, ConfigError, load_config, scan

Symlink = Callable[[Path, Path], None]


def _store(tmp_path: Path, config: Optional[Union[str, dict]] = None) -> StoreDir:
    root = tmp_path / "store"
    root.mkdir()
    if config is not None:
        text = config if isinstance(config, str) else json.dumps(config)
        (root / ".memory-index.json").write_text(text, encoding="utf-8")
    return StoreDir(str(root))


def test_config_defaults(tmp_path: Path) -> None:
    with _store(tmp_path) as sd:
        cfg = load_config(sd)
    assert cfg == Config()
    assert (cfg.budget_bytes, cfg.budget_lines, cfg.item_max_bytes, cfg.line_max_bytes) == (18432, 135, 300, 230)
    assert (cfg.l1_name, cfg.lock_timeout_s) == ("index-l1.md", 5)


def test_config_values_and_inclusive_bounds(tmp_path: Path) -> None:
    values = {"budget_bytes": 25000, "budget_lines": 20, "item_max_bytes": 80, "line_max_bytes": 2000,
              "l1_name": "index-more.md", "lock_timeout_s": 1.5}
    with _store(tmp_path, values) as sd:
        assert load_config(sd) == Config(**values)


def test_config_accepts_bom(tmp_path: Path) -> None:
    with _store(tmp_path, '\ufeff{"lock_timeout_s": 60}') as sd:
        assert load_config(sd).lock_timeout_s == 60


@pytest.mark.parametrize(
    "config",
    [
        {"bogus": 1},
        {"budget_bytes": "18432"},
        {"budget_bytes": 18432.0},
        {"budget_lines": True},
        {"budget_bytes": 2047},
        {"budget_bytes": 25001},
        {"budget_lines": 19},
        {"budget_lines": 201},
        {"item_max_bytes": 79},
        {"item_max_bytes": 2001},
        {"line_max_bytes": 79},
        {"line_max_bytes": 2001},
        {"lock_timeout_s": 0.5},
        {"lock_timeout_s": 61},
        {"lock_timeout_s": "5"},
        {"l1_name": "../x.md"},
        {"l1_name": "x.txt"},
        {"l1_name": ".x.md"},
        {"l1_name": "MEMORY.md"},
        {"l1_name": "memory.md"},
        {"l1_name": 3},
        "[1, 2]",
        "{not json",
        '{"lock_timeout_s": NaN}',
        '{"budget_bytes": 3000, "budget_bytes": 4000}',
    ],
)
def test_config_errors(tmp_path: Path, config: Union[str, dict]) -> None:
    with _store(tmp_path, config) as sd, pytest.raises(ConfigError):
        load_config(sd)


def test_config_link_is_refused(tmp_path: Path, symlink: Symlink) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    with _store(tmp_path) as sd:
        symlink(Path(sd.path) / ".memory-index.json", outside)
        with pytest.raises(ConfigError):
            load_config(sd)


def test_config_construction_validates() -> None:
    with pytest.raises(ConfigError):
        Config(budget_bytes=1)


def _write(root: Path, name: str, data: Union[str, bytes]) -> None:
    (root / name).write_bytes(data.encode() if isinstance(data, str) else data)


def test_scan_reads_sources_and_reports_skips(tmp_path: Path, symlink: Symlink) -> None:
    with _store(tmp_path) as sd:
        root = Path(sd.path)
        _write(root, "b.md", "---\nname: b\n---\nB\n")
        _write(root, "a.md", "no frontmatter\n")
        for name in ("MEMORY.md", "index-l1.md", "notes.txt", ".index.lock"):
            _write(root, name, "x")
        _write(root, ".hidden.md", "x")
        _write(root, "bad name.md", "x")
        _write(root, "big.md", b"x" * (store.SOURCE_MAX_BYTES + 1))
        _write(root, "bin.md", b"\xff\xfe")
        (root / "dir.md").mkdir()
        _write(tmp_path, "outside.md", "secret")
        symlink(root / "link.md", tmp_path / "outside.md")
        res = scan(sd, Config(), {"MEMORY.md", "index-l1.md"})
    assert [s.name for s in res.sources] == ["a.md", "b.md"]
    a, b = res.sources
    assert a.text == "no frontmatter\n" and not a.fm.present
    assert a.fp == Fingerprint(15, hashlib.sha256(b"no frontmatter\n").hexdigest())
    assert b.fm.get("name") == "b"
    skipped = dict(res.skipped)
    assert sorted(skipped) == [".hidden.md", "bad name.md", "big.md", "bin.md", "dir.md", "link.md"]
    assert "link" in skipped["link.md"] and "KiB" in skipped["big.md"] and "UTF-8" in skipped["bin.md"]
    assert all(reason for reason in skipped.values())
    assert (tmp_path / "outside.md").read_text() == "secret"


def test_scan_excludes_generated_names_case_insensitively(tmp_path: Path) -> None:
    with _store(tmp_path) as sd:
        _write(Path(sd.path), "Index-L1.md", "x")
        _write(Path(sd.path), "keep.md", "x")
        res = scan(sd, Config(), set())
    assert [s.name for s in res.sources] == ["keep.md"] and res.skipped == []


def test_scan_source_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert store.MAX_SOURCES == 5000
    monkeypatch.setattr(store, "MAX_SOURCES", 3)
    with _store(tmp_path) as sd:
        for name in ("e.md", "d.md", "c.md", "b.md", "a.md"):
            _write(Path(sd.path), name, name)
        res = scan(sd, Config(), set())
    assert [s.name for s in res.sources] == ["a.md", "b.md", "c.md"]
    assert [n for n, _ in res.skipped] == ["d.md", "e.md"]
    assert all("limit" in reason for _, reason in res.skipped)
