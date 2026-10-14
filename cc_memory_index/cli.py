"""Command line: cc-memory-index <command> [--dir PATH] [--json] (DESIGN §10).

stdout carries a short summary, or one JSON object with --json; stderr carries errors and
warnings only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from . import __version__, hook, migrate
from .build import EXIT_OK, EXIT_USAGE, BuildResult, run_build, run_check
from .demote import run_demote
from .lint import run_lint
from .store import ENV_STORE, L0_NAME, StoreNotFound, discover
from .suggest import run_suggest


@dataclass(frozen=True)
class Command:
    help: str
    run: Callable[[str, argparse.Namespace], BuildResult]
    configure: Optional[Callable[[argparse.ArgumentParser], None]] = None
    # When set, the command's messages are its output: they go to stdout through this report.
    report: Optional[Callable[[BuildResult], list[str]]] = None


def _build_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--force", action="store_true",
        help=f"overwrite hand-edited generated files after backing up {L0_NAME}",
    )


def _suggest_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--transcripts", type=int, metavar="N", choices=range(1, 51),
                        help="also rank by mentions in the N most recent transcripts of this project (1-50)")
    parser.add_argument("--apply", action="store_true", help="demote the proposed memories and rebuild")


def _demote_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", help="memory file to move out of the resident index (sets tier: indexed)")


def _migrate_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="convert the store (a snapshot is taken first)")
    group.add_argument("--restore", metavar="SNAPSHOT", help="undo a migration from its snapshot")


def _with_exit(lines: list[str], command: str, res: BuildResult) -> list[str]:
    return lines + ([f"{command}: exit {res.code}"] if res.code != EXIT_OK else [])


def _lint_report(res: BuildResult) -> list[str]:
    s = res.stats
    tally = f"lint: {s.get('errors', 0)} error(s), {s.get('warnings', 0)} warning(s)" if "errors" in s else ""
    return _with_exit(res.messages + ([tally] if tally else []), "lint", res)


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
                    lambda store, args: run_lint(store), report=_lint_report),
    "suggest": Command(
        "propose memories to demote when the resident index is over budget",
        lambda store, args: run_suggest(store, args.transcripts, args.apply), _suggest_options,
        lambda res: _with_exit(list(res.messages), "suggest", res),
    ),
    "demote": Command(
        "set tier: indexed on one memory file and rebuild",
        lambda store, args: run_demote(store, args.file), _demote_options,
        lambda res: res.messages + _summary("demote", res),
    ),
    "migrate": Command(
        f"convert a hand-written {L0_NAME} (dry run unless --apply)",
        lambda store, args: migrate.run_migrate(store, args.apply, args.restore), _migrate_options,
        lambda res: res.messages + migrate.summary(res),
    ),
}
HOOK_HELP = "Claude Code PostToolUse hook: reads the hook payload on stdin (used by the plugin)"



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


def _summary(command: str, res: BuildResult) -> list[str]:
    s = res.stats
    if "sources" not in s:
        return [f"{command}: exit {res.code}"]
    lines = [
        f"{command}: {s['sources']} memory files; {s['l0_items']} of {s['resident']} resident entries in {L0_NAME},"
        f" {s['l1_items']} in {s['l1_name']}; {s['manual']} manual lines, {s['pending']} pending",
        f"{L0_NAME}: {s['l0_bytes']:,} of {s['budget_bytes']:,} bytes, {s['l0_lines']} of {s['budget_lines']} lines;"
        f" overflow {s['overflow']}, unresolved {s['unresolved']}",
    ]
    if "written" in s:
        lines.append("wrote " + ", ".join(s["written"]) if s["written"] else "up to date: nothing written")
    if "out_of_sync" in s:
        stale = s["out_of_sync"]
        lines.append(f"out of sync: {', '.join(stale)}; run `cc-memory-index build`" if stale else "in sync")
    if res.code != EXIT_OK:
        lines.append(f"exit {res.code}")
    return lines


def _emit(command: str, res: BuildResult, as_json: bool) -> None:
    if as_json:
        obj = {"command": command, "code": res.code, "messages": res.messages, "stats": res.stats}
        print(json.dumps(obj))
        return
    report = COMMANDS[command].report if command in COMMANDS else None
    if report is not None:
        for line in report(res):
            print(line)
        return
    for message in res.messages:
        print(message, file=sys.stderr)
    for line in _summary(command, res):
        print(line)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = make_parser().parse_args(argv)
    if args.command == "hook":
        return hook.main()
    as_json = bool(getattr(args, "json", False))
    try:
        store = discover(getattr(args, "dir", None), os.getcwd(), os.environ)
    except StoreNotFound as e:
        print(e, file=sys.stderr)
        if as_json:
            _emit(args.command, BuildResult(EXIT_USAGE, [str(e)]), True)
        return EXIT_USAGE
    res = COMMANDS[args.command].run(store, args)
    _emit(args.command, res, as_json)
    return res.code
