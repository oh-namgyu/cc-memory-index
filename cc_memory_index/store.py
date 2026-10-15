"""Store discovery, source names, configuration and the source scan (DESIGN §2, §9-§11)."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import asdict, dataclass, fields
from typing import AbstractSet, Mapping, Optional

from .frontmatter import Frontmatter, parse
from .safeio import Fingerprint, SafeIOError, StoreDir, TooLarge, UnsafeEntry

SOURCE_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]*\.md\Z")
# Windows opens a device, not a file, for these names with any extension (CON.md is CON).
_DEVICE_NAMES = frozenset(["CON", "PRN", "AUX", "NUL"] + [f"{port}{n}" for port in ("COM", "LPT") for n in "123456789"])
MAX_SOURCES = 5000
SOURCE_MAX_BYTES = 1024 * 1024
L0_NAME = "MEMORY.md"
CONFIG_NAME = ".memory-index.json"
CONFIG_MAX_BYTES = 64 * 1024
ENV_STORE = "CC_MEMORY_INDEX_DIR"
ENV_CONFIG = "CLAUDE_CONFIG_DIR"
RENDER_MARK = ".index-render.json"
GITFILE_MAX_BYTES = 4096
_RANGES = {
    "budget_bytes": (2048, 25000),
    "budget_lines": (20, 200),
    "item_max_bytes": (80, 4096),
    "lock_timeout_s": (1, 60),
}


class StoreNotFound(Exception):
    """No memory store directory could be found."""


class ConfigError(Exception):
    """.memory-index.json is unreadable, malformed or out of range."""


def valid_source_name(name: str) -> bool:
    """DESIGN §2: a plain name matching SOURCE_NAME_RE, without '..', whose part before the first '.'
    is not a Windows device name (any case)."""
    return (
        isinstance(name, str)
        and SOURCE_NAME_RE.match(name) is not None
        and ".." not in name
        and name.split(".", 1)[0].upper() not in _DEVICE_NAMES
    )


def _home(env: Mapping[str, str]) -> str:
    keys = ("USERPROFILE", "HOME") if os.name == "nt" else ("HOME",)
    for key in keys:
        if env.get(key):
            return env[key]
    return os.path.expanduser("~")


def config_dir(env: Mapping[str, str]) -> str:
    """$CLAUDE_CONFIG_DIR, else ~/.claude, as an absolute path."""
    configured = env.get(ENV_CONFIG)
    path = os.path.expanduser(configured) if configured else os.path.join(_home(env), ".claude")
    return os.path.abspath(path)


def project_key(path: str) -> str:
    """A project directory with every non-alphanumeric character replaced by '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def repo_root(start: str) -> str:
    """The directory Claude Code keys auto memory on.

    That is the git repository containing start, shared by all its worktrees and
    subdirectories; outside a repository it is start itself.
    """
    here = os.path.abspath(start)
    while True:
        dot_git = os.path.join(here, ".git")
        if os.path.isdir(dot_git):
            return here
        if os.path.isfile(dot_git):
            return _main_worktree(here, dot_git)
        parent = os.path.dirname(here)
        if parent == here:
            return os.path.abspath(start)
        here = parent


def _main_worktree(here: str, dot_git: str) -> str:
    """A linked worktree's .git file names <main>/.git/worktrees/<id>; key on <main>.

    Any other .git file (a submodule) keys on its own directory.
    """
    try:
        with open(dot_git, "rb") as f:
            head = f.read(GITFILE_MAX_BYTES).decode("utf-8", "replace")
    except OSError:
        return here
    first = head.split("\n", 1)[0].rstrip("\r")
    if not first.startswith("gitdir:"):
        return here
    gitdir = os.path.normpath(os.path.join(here, first[len("gitdir:"):].strip()))
    worktrees = os.path.dirname(gitdir)
    if os.path.basename(worktrees) == "worktrees" and os.path.basename(os.path.dirname(worktrees)) == ".git":
        return os.path.dirname(os.path.dirname(worktrees))
    return here


def discover(dir_arg: Optional[str], cwd: str, env: Mapping[str, str]) -> str:
    """Store directory from --dir, then $CC_MEMORY_INDEX_DIR, then the project of cwd."""
    if dir_arg:
        origin, raw = "--dir", dir_arg
    elif env.get(ENV_STORE):
        origin, raw = ENV_STORE, env[ENV_STORE]
    else:
        root = repo_root(cwd)
        origin = f"the project of {root}"
        raw = os.path.join(config_dir(env), "projects", project_key(root), "memory")
    path = os.path.abspath(os.path.join(cwd, os.path.expanduser(raw)))
    if not os.path.isdir(path):
        raise StoreNotFound(
            f"no memory store directory at {path} (from {origin}); pass --dir PATH or set {ENV_STORE}"
            " (needed when autoMemoryDirectory or CLAUDE_CODE_PROJECT_DIR_NAME is set)"
        )
    return path


def store_root_for_edit(file_path: str) -> Optional[str]:
    """The store an edited file sits directly inside, or None (DESIGN §11).

    Only stores this tool has written at least once qualify: they hold RENDER_MARK. Installing
    the hook therefore never touches a store the user has not opted in, wherever it lives.
    """
    if not isinstance(file_path, str) or not file_path.endswith(".md") or "\x00" in file_path:
        return None
    parent = os.path.dirname(os.path.abspath(file_path))
    try:
        st = os.lstat(os.path.join(parent, RENDER_MARK))
    except OSError:
        return None
    return parent if stat.S_ISREG(st.st_mode) else None


def _check_field(key: str, value: object) -> None:
    if key == "l1_name":
        if not isinstance(value, str) or not valid_source_name(value) or value.lower() == L0_NAME.lower():
            raise ConfigError(f"l1_name must be a valid source name other than {L0_NAME}, not {value!r}")
        return
    low, high = _RANGES[key]
    kinds = (int, float) if key == "lock_timeout_s" else (int,)
    if isinstance(value, bool) or not isinstance(value, kinds):
        raise ConfigError(f"{key} must be {'a number' if float in kinds else 'an integer'}, not {value!r}")
    if not low <= value <= high:
        raise ConfigError(f"{key} must be between {low} and {high}, not {value!r}")


@dataclass(frozen=True)
class Config:
    budget_bytes: int = 20480
    budget_lines: int = 160
    item_max_bytes: int = 1024
    l1_name: str = "index-l1.md"
    lock_timeout_s: float = 5

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            _check_field(key, value)


def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key {key!r}")
        obj[key] = value
    return obj


def load_config(sd: StoreDir) -> Config:
    """Config from .memory-index.json in the store, or the defaults when it is absent."""
    if sd.kind(CONFIG_NAME) == "missing":
        return Config()
    try:
        data, _ = sd.read(CONFIG_NAME, CONFIG_MAX_BYTES)
        obj = json.loads(data.decode("utf-8-sig"), object_pairs_hook=reject_duplicates)
    except SafeIOError as e:
        raise ConfigError(f"cannot read {CONFIG_NAME}: {e}") from e
    except (ValueError, RecursionError) as e:
        raise ConfigError(f"{CONFIG_NAME} is not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ConfigError(f"{CONFIG_NAME} must contain a JSON object")
    unknown = sorted(set(obj) - {f.name for f in fields(Config)})
    if unknown:
        raise ConfigError(f"unknown key(s) in {CONFIG_NAME}: {', '.join(unknown)}")
    return Config(**obj)


@dataclass
class Source:
    name: str
    text: str
    fp: Fingerprint
    fm: Frontmatter


@dataclass
class ScanResult:
    sources: list[Source]
    skipped: list[tuple[str, str]]


def skip_reason(sd: StoreDir, name: str, count: int) -> Optional[str]:
    if not valid_source_name(name):
        return "invalid source name"
    found = sd.kind(name)
    if found == "link":
        return "symbolic link or reparse point (never followed)"
    if found != "file":
        return f"not a regular file ({found})"
    if count >= MAX_SOURCES:
        return f"over the limit of {MAX_SOURCES:,} sources"
    return None


def load_source(sd: StoreDir, name: str) -> tuple[Optional[Source], Optional[str]]:
    try:
        data, fp = sd.read(name, SOURCE_MAX_BYTES)
    except TooLarge:
        return None, f"larger than {SOURCE_MAX_BYTES // 1024} KiB"
    except UnsafeEntry as e:
        return None, str(e)
    except FileNotFoundError:
        return None, "removed while scanning"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not valid UTF-8"
    return Source(name, text, fp, parse(text)), None


def scan(sd: StoreDir, cfg: Config, generated: AbstractSet[str] = frozenset()) -> ScanResult:
    """Read every source in name order; links are never followed and every skip has a reason.

    MEMORY.md, cfg.l1_name and the other generated names given are excluded, compared
    case-insensitively because the store may live on a case-insensitive file system.
    """
    reserved = {n.lower() for n in generated} | {L0_NAME.lower(), cfg.l1_name.lower()}
    result = ScanResult([], [])
    for name in sd.names():
        if not name.endswith(".md") or name.lower() in reserved:
            continue
        reason = skip_reason(sd, name, len(result.sources))
        source = None
        if reason is None:
            source, reason = load_source(sd, name)
        if source is not None:
            result.sources.append(source)
        else:
            result.skipped.append((name, reason or "unreadable"))
    return result
