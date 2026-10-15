"""Command line: cc-memory-index <command> [--dir PATH] [--json] (DESIGN §10).

A command prints its whole report (its messages, a summary, then `<command>: exit N` unless N is
0) on stdout when it exits 0 or 1, and on stderr when it exits 2 or more. With --json it prints
exactly one JSON object on stdout and nothing on stderr. Characters the output encoding cannot
represent are backslash-escaped, and a reader that goes away early is not an error.
"""

from __future__ import annotations

import argparse
import errno
import io
import json
import os
import sys
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, TextIO

from . import __version__, hook, migrate
from .build import EXIT_OK, EXIT_OVERFLOW, EXIT_USAGE, BuildResult, run_build, run_check
from .demote import run_demote
from .lint import run_lint
from .migrate_snapshot import SNAPSHOT_PREFIX
from .store import ENV_STORE, L0_NAME, StoreNotFound, discover
from .suggest import run_suggest
from .transcripts import MAX_TRANSCRIPTS


def _stats_summary(name: str, res: BuildResult) -> list[str]:
    """What build, check and demote rendered, and what they wrote or found out of date."""
    s = res.stats
    if "sources" not in s:
        return []
    lines = [
        f"{name}: {s['sources']} memory files; {s['l0_items']} of {s['resident']} resident entries in {L0_NAME},"
        f" {s['l1_items']} in {s['l1_name']}; {s['manual']} manual lines, {s['pending']} pending",
        f"{L0_NAME}: {s['l0_bytes']:,} of {s['budget_bytes']:,} bytes, {s['l0_lines']} of {s['budget_lines']} lines;"
        f" overflow {s['overflow']}, unresolved {s['unresolved']}",
    ]
    if "written" in s:
        lines.append("wrote " + ", ".join(s["written"]) if s["written"] else "up to date: nothing written")
    if "out_of_sync" in s:
        stale = s["out_of_sync"]
        lines.append(f"out of sync: {', '.join(stale)}; run `cc-memory-index build`" if stale else "in sync")
    return lines


def _lint_tally(name: str, res: BuildResult) -> list[str]:
    s = res.stats
    return [f"{name}: {s['errors']} error(s), {s.get('warnings', 0)} warning(s)"] if "errors" in s else []


@dataclass(frozen=True)
class Command:
    help: str
    run: Callable[[str, argparse.Namespace], BuildResult]
    configure: Optional[Callable[[argparse.ArgumentParser], None]] = None
    # The lines printed after the command's messages, before the exit line.
    summary: Callable[[str, BuildResult], list[str]] = _stats_summary


def _transcripts(value: str) -> int:
    """--transcripts N: a whole number from 1 to MAX_TRANSCRIPTS."""
    try:
        n = int(value)
    except ValueError:
        n = 0
    if not 1 <= n <= MAX_TRANSCRIPTS:
        raise argparse.ArgumentTypeError(f"expected a whole number from 1 to {MAX_TRANSCRIPTS}, not {value!r}")
    return n


def _snapshot(value: str) -> str:
    """--restore SNAPSHOT: the snapshot id, or the name of its directory (.index-backup-<id>)."""
    name = value.rstrip("/\\")
    return name[len(SNAPSHOT_PREFIX):] if name.startswith(SNAPSHOT_PREFIX) else name


def _build_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--force", action="store_true",
        help=f"overwrite hand edits instead of keeping them, after backing up {L0_NAME} and the L1 file",
    )


def _suggest_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--transcripts", type=_transcripts, metavar="N",
        help=f"also rank by mentions in the N most recent transcripts of this project (1-{MAX_TRANSCRIPTS})",
    )
    parser.add_argument("--apply", action="store_true", help="demote the proposed memories and rebuild")


def _demote_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", help="memory file to move out of the resident index (sets tier: indexed)")


def _migrate_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="convert the store (a snapshot is taken first)")
    group.add_argument("--restore", metavar="SNAPSHOT", type=_snapshot,
                       help=f"undo a migration; SNAPSHOT is the id that --apply printed, or {SNAPSHOT_PREFIX}<id>")


COMMANDS: dict[str, Command] = {
    "build": Command(
        f"render {L0_NAME} and the L1 file from the memory files' frontmatter",
        lambda store, args: run_build(store, args.force), _build_options,
    ),
    "check": Command(
        "exit 1 when the generated files are out of date; writes nothing",
        lambda store, args: run_check(store),
    ),
    "lint": Command("report broken frontmatter, links and stale index entries; writes nothing",
                    lambda store, args: run_lint(store), summary=_lint_tally),
    "suggest": Command(
        "propose memories to demote when the resident index is over budget",
        lambda store, args: run_suggest(store, args.transcripts, args.apply), _suggest_options,
        lambda name, res: [],
    ),
    "demote": Command(
        "set tier: indexed on one memory file and rebuild",
        lambda store, args: run_demote(store, args.file), _demote_options,
    ),
    "migrate": Command(
        f"convert a hand-written {L0_NAME} (dry run unless --apply)",
        lambda store, args: migrate.run_migrate(store, args.apply, args.restore), _migrate_options,
        lambda name, res: migrate.summary(res),
    ),
}
HOOK_HELP = "Claude Code PostToolUse hook: reads the hook payload on stdin (used by the plugin; takes no options)"


def _common() -> argparse.ArgumentParser:
    """--dir and --json, accepted before or after the command name."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", metavar="PATH", default=argparse.SUPPRESS,
                        help=f"memory store directory (default: ${ENV_STORE}, then the current project's store)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print one JSON object instead of a summary")
    return common


def make_parser() -> argparse.ArgumentParser:
    common = _common()
    top = argparse.ArgumentParser(
        prog="cc-memory-index", parents=[common],
        description="Generate Claude Code's auto-memory index from each memory file's frontmatter.",
    )
    top.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = top.add_subparsers(dest="command", metavar="COMMAND")
    subs.required = True
    for name, command in COMMANDS.items():
        sub = subs.add_parser(name, help=command.help, description=command.help, parents=[common])
        if command.configure is not None:
            command.configure(sub)
    subs.add_parser("hook", help=HOOK_HELP, description=HOOK_HELP)
    return top


def report(name: str, res: BuildResult) -> list[str]:
    """The human report: messages, the command's summary, then the exit line unless the exit is 0."""
    lines = res.messages + COMMANDS[name].summary(name, res)
    exit_line = f"{name}: exit {res.code}"
    # migrate.summary ends with its own exit line; it is not repeated.
    if res.code != EXIT_OK and lines[-1:] != [exit_line]:
        lines.append(exit_line)
    return lines


def _output(code: int) -> TextIO:
    """Exit 0 or 1 is a result (1: overflow, out of sync, lint errors) for stdout; 2 or more is a failure."""
    return sys.stdout if code in (EXIT_OK, EXIT_OVERFLOW) else sys.stderr


def _emit(name: str, res: BuildResult, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"command": name, "code": res.code, "messages": res.messages, "stats": res.stats}))
        return
    stream = _output(res.code)
    for line in report(name, res):
        print(line, file=stream)


def _parse(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.command == "hook" and ("dir" in args or "json" in args):
        parser.error("hook takes no --dir or --json: it finds the store from the edited file in its stdin payload")
    return args


def _execute(args: argparse.Namespace) -> BuildResult:
    try:
        store = discover(args.dir if "dir" in args else None, os.getcwd(), os.environ)
    except StoreNotFound as e:
        return BuildResult(EXIT_USAGE, [str(e)])
    return COMMANDS[args.command].run(store, args)


def _reader_gone(e: OSError) -> bool:
    """A write to stdout failed because its reader exited (EINVAL instead of EPIPE on Windows)."""
    return isinstance(e, BrokenPipeError) or (os.name == "nt" and e.errno == errno.EINVAL)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run one command and return its exit code; argparse exits 2 on a usage error."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")
    code = EXIT_OK
    try:
        try:
            args = _parse(argv)
            if args.command == "hook":
                return hook.main()
            res = _execute(args)
            code = res.code
            _emit(args.command, res, "json" in args)
        finally:
            sys.stdout.flush()
    except OSError as e:
        if not _reader_gone(e):
            raise
        hook.discard(sys.stdout)
    return code
