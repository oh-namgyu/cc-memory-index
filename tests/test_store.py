from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import pytest

from cc_memory_index.store import (
    SOURCE_NAME_RE,
    StoreNotFound,
    config_dir,
    discover,
    project_key,
    repo_root,
    store_root_for_edit,
    valid_source_name,
)

Symlink = Callable[[Path, Path], None]


@pytest.mark.parametrize(
    "name, ok",
    [
        ("ok-1.md", True),
        ("build_commands.md", True),
        ("A.b.md", True),
        ("MEMORY.md", True),
        ("..md", False),
        ("a..b.md", False),
        ("a/b.md", False),
        ("a\\b.md", False),
        (".hidden.md", False),
        ("x.txt", False),
        ("x.MD", False),
        (".md", False),
        ("-x.md", False),
        ("_x.md", False),
        ("x y.md", False),
        ("x.md\n", False),
        ("메모.md", False),
        ("", False),
        ("CON.md", False),
        ("con.md", False),
        ("Nul.md", False),
        ("prn.md", False),
        ("AUX.notes.md", False),
        ("COM1.md", False),
        ("lpt9.md", False),
        ("COM0.md", True),
        ("COM10.md", True),
        ("CONSOLE.md", True),
        ("acon.md", True),
        ("con-x.md", True),
    ],
)
def test_source_name_rule(name: str, ok: bool) -> None:
    assert valid_source_name(name) is ok


def test_source_name_regex_is_anchored() -> None:
    assert SOURCE_NAME_RE.match("a.md\n") is None
    assert SOURCE_NAME_RE.search("../a.md") is None


def test_project_key() -> None:
    assert project_key("/Users/me/my.repo") == "-Users-me-my-repo"
    assert project_key("C:\\work\\a b") == "C--work-a-b"
    assert project_key("/a/메모") == "-a---"
    assert project_key("abc123") == "abc123"


def test_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert config_dir({"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}) == str(tmp_path / "cfg")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert config_dir(dict(os.environ)) == str(home / ".claude")
    assert config_dir({}) == str(home / ".claude")


def _project_store(cfg: Path, cwd: Path) -> Path:
    path = cfg / "projects" / project_key(str(cwd)) / "memory"
    path.mkdir(parents=True)
    return path


def test_discover_precedence(tmp_path: Path) -> None:
    cfg, cwd = tmp_path / "cfg", tmp_path / "work dir"
    cwd.mkdir()
    auto = _project_store(cfg, cwd)
    pinned, explicit = tmp_path / "pinned", tmp_path / "explicit"
    pinned.mkdir()
    explicit.mkdir()
    env = {"CLAUDE_CONFIG_DIR": str(cfg), "CC_MEMORY_INDEX_DIR": str(pinned)}
    assert discover(str(explicit), str(cwd), env) == str(explicit)
    assert discover("explicit", str(tmp_path), env) == str(explicit)
    assert discover(None, str(cwd), env) == str(pinned)
    del env["CC_MEMORY_INDEX_DIR"]
    assert discover(None, str(cwd), env) == str(auto)
    assert discover("", str(cwd), env) == str(auto)


def test_discover_uses_home_when_config_dir_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, cwd = tmp_path / "home", tmp_path / "proj"
    cwd.mkdir()
    auto = _project_store(home / ".claude", cwd)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CC_MEMORY_INDEX_DIR", raising=False)
    assert discover(None, str(cwd), dict(os.environ)) == str(auto)


def test_discover_store_not_found(tmp_path: Path) -> None:
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}
    with pytest.raises(StoreNotFound, match="--dir"):
        discover(None, str(tmp_path), env)
    (tmp_path / "file").write_text("x", encoding="utf-8")
    with pytest.raises(StoreNotFound):
        discover(str(tmp_path / "file"), str(tmp_path), env)
    with pytest.raises(StoreNotFound, match="CC_MEMORY_INDEX_DIR"):
        discover(None, str(tmp_path), {**env, "CC_MEMORY_INDEX_DIR": str(tmp_path / "nope")})


def _marked(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".index-render.json").write_text("{}", encoding="utf-8")
    return path


def test_store_root_for_edit_needs_the_opt_in_mark(tmp_path: Path) -> None:
    mem = _marked(tmp_path / "cfg" / "projects" / "-k" / "memory")
    assert store_root_for_edit(str(mem / "a.md")) == str(mem)
    assert store_root_for_edit(str(mem / "MEMORY.md")) == str(mem)
    assert store_root_for_edit(str(mem / "sub" / ".." / "a.md")) == str(mem)
    unmarked = tmp_path / "cfg" / "projects" / "-j" / "memory"
    unmarked.mkdir(parents=True)
    assert store_root_for_edit(str(unmarked / "a.md")) is None


def test_store_root_for_edit_anywhere_once_marked(tmp_path: Path) -> None:
    custom = _marked(tmp_path / "custom-memory-dir")
    assert store_root_for_edit(str(custom / "note.md")) == str(custom)


def test_store_root_for_edit_negative(tmp_path: Path) -> None:
    mem = _marked(tmp_path / "mem")
    (mem / "sub").mkdir()
    for path in (mem / "a.txt", mem / "a.md.bak", mem / "sub" / "a.md", tmp_path / "gone" / "a.md"):
        assert store_root_for_edit(str(path)) is None, path
    assert store_root_for_edit("") is None
    assert store_root_for_edit(str(mem) + "/a\x00.md") is None
    assert store_root_for_edit(None) is None  # type: ignore[arg-type]


def test_store_root_for_edit_rejects_a_linked_or_non_file_mark(tmp_path: Path, symlink: Symlink) -> None:
    linked = tmp_path / "linked"
    linked.mkdir()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    symlink(linked / ".index-render.json", target)
    assert store_root_for_edit(str(linked / "a.md")) is None
    dir_mark = tmp_path / "dirmark"
    (dir_mark / ".index-render.json").mkdir(parents=True)
    assert store_root_for_edit(str(dir_mark / "a.md")) is None


def test_store_root_for_edit_symlinked_store(tmp_path: Path, symlink: Symlink) -> None:
    real = _marked(tmp_path / "synced" / "store")
    link = tmp_path / "link"
    symlink(link, real)
    assert store_root_for_edit(str(real / "a.md")) == str(real)
    assert store_root_for_edit(str(link / "a.md")) == str(link)


def _git_repo(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    return root


def test_repo_root_plain_subdir_worktree_and_submodule(tmp_path: Path) -> None:
    main = _git_repo(tmp_path / "main")
    (main / "src" / "deep").mkdir(parents=True)
    assert repo_root(str(main / "src" / "deep")) == str(main)
    wt = tmp_path / "wt"
    wt.mkdir()
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n", encoding="utf-8")
    assert repo_root(str(wt)) == str(main)
    sub = main / "vendor" / "lib"
    sub.mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../../.git/modules/lib\n", encoding="utf-8")
    assert repo_root(str(sub)) == str(sub)
    odd = tmp_path / "odd"
    odd.mkdir()
    (odd / ".git").write_text("not a gitdir line", encoding="utf-8")
    assert repo_root(str(odd)) == str(odd)
    plain = tmp_path / "plain" / "x"
    plain.mkdir(parents=True)
    assert repo_root(str(plain)) == str(plain)


def test_discover_keys_on_the_repository(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    main = _git_repo(tmp_path / "main")
    (main / "src").mkdir()
    store = _project_store(cfg, main)
    assert discover(None, str(main / "src"), {"CLAUDE_CONFIG_DIR": str(cfg)}) == str(store)
