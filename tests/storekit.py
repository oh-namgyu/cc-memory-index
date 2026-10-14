"""Synthetic memory stores for the tests (never a real ~/.claude store)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional, Union

from cc_memory_index.build import run_build
from cc_memory_index.frontmatter import parse
from cc_memory_index.items import Item
from cc_memory_index.safeio import Fingerprint
from cc_memory_index.store import Source

TOP_KEYS = ("name", "description")


def _quote(value: Union[str, int]) -> str:
    if isinstance(value, int):
        return str(value)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def memory_text(body: str = "body\n", /, **meta: Union[str, int]) -> str:
    """A memory file: name/description at the top level, everything else under metadata."""
    lines = ["---"] + [f"{k}: {_quote(v)}" for k, v in meta.items() if k in TOP_KEYS]
    rest = [f"  {k}: {_quote(v)}" for k, v in meta.items() if k not in TOP_KEYS]
    if rest:
        lines += ["metadata:"] + rest
    return "\n".join(lines + ["---", body])


def source(file: str, /, **meta: Union[str, int]) -> Source:
    text = memory_text(**meta)
    data = text.encode()
    return Source(file, text, Fingerprint(len(data), hashlib.sha256(data).hexdigest()), parse(text))


def item(idx: str, section: str = "S", sub: str = "", order: int = 0, file: Optional[str] = None,
         resident: bool = True, origin: str = "source") -> Item:
    if file is None and origin != "manual":
        file = hashlib.sha256(idx.encode()).hexdigest()[:12] + ".md"
    return Item(file, idx, section, sub, order, resident, origin)


def make_store(tmp_path: Path, config: Optional[dict[str, Any]] = None) -> Path:
    root = tmp_path / "store"
    root.mkdir()
    if config is not None:
        (root / ".memory-index.json").write_text(json.dumps(config), encoding="utf-8")
    return root


def write_memory(root: Path, file: str, /, **meta: Union[str, int]) -> Path:
    path = root / file
    path.write_text(memory_text(**meta), encoding="utf-8")
    return path


def write_json(root: Path, name: str, obj: Any) -> None:
    (root / name).write_text(json.dumps(obj), encoding="utf-8")


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every regular file's bytes and inode (a replace always changes the inode); the lock file
    is left out because taking the lock creates it."""
    out: dict[str, tuple[bytes, int]] = {}
    for name in sorted(set(os.listdir(root)) - {".index.lock"}):
        path = root / name
        if path.is_file() and not path.is_symlink():
            out[name] = (path.read_bytes(), os.stat(path).st_ino)
    return out


def sample_store(tmp_path: Path, config: Optional[dict[str, Any]] = None) -> Path:
    """Two resident memories and one on-demand memory."""
    root = make_store(tmp_path, config)
    write_memory(root, "build.md", name="Build", description="make test", type="reference", tier="resident",
                 idx_order=40)
    write_memory(root, "tea.md", name="Tea", description="likes tea", type="user", tier="resident")
    write_memory(root, "old.md", name="Old", description="an old project note", type="project")
    return root


def built_store(tmp_path: Path, config: Optional[dict[str, Any]] = None) -> Path:
    root = sample_store(tmp_path, config)
    assert run_build(str(root), False).code == 0
    return root
