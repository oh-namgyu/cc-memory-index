"""scripts/check_forbidden.py: bypasses found in review must be caught, the package must pass."""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import ROOT

GATE: dict[str, Any] = runpy.run_path(os.path.join(ROOT, "scripts", "check_forbidden.py"))

# Every sample below is source text that the gate parses with ast; none of it is ever executed.
# The review's sample files, verbatim, and the lines each must be flagged on.
AUDIT_SAMPLES = [
    ("import os, importlib, multiprocessing, asyncio, runpy, pty, webbrowser, ftplib, smtplib\n"
     'os.posix_spawn("/bin/sh", ["sh"], {})\n'
     "os.forkpty()\n"
     'importlib.import_module("subprocess").run(["id"])\n'
     'getattr(os, "sys" + "tem")("id")\n'
     'asyncio.create_subprocess_exec("id")\n'
     'runpy.run_path("x.py")\n'
     'builtins = __builtins__; builtins["ev" + "al"]("1")\n'
     "from urllib.request import urlopen\n"
     "import urllib.request\n", {1, 2, 3, 5, 8, 9, 10}),
    ("import os, subprocess\nimport json as j, socket\nfrom os import system\nfrom . import subprocess_shim\n",
     {1, 2, 3, 4}),
]
BYPASSES = [
    "import os, subprocess", "import json as j, socket", "from os import system", "from os import *",
    "from . import subprocess_shim", "from .. import build", "from cc_memory_index import subprocess_shim",
    "import importlib", "import importlib.util", "import multiprocessing", "import asyncio", "import runpy",
    "import pty", "import ctypes", "import pickle", "import shutil", "import signal", "import webbrowser",
    "import ftplib", "import smtplib", "import xmlrpc.client", "import urllib.request", "from http import client",
    "import os\nos.posix_spawn('/bin/sh', ['sh'], {})", "import os\nos.forkpty()", "import os\nos.fork()",
    "import os\nos.system('id')", "import os\nos.popen('id')", "import os\nos.execv('/bin/sh', ['sh'])",
    "import os\nos.spawnlp(os.P_WAIT, 'id', 'id')", "import os\nos.startfile('x')", "import os\nos.kill(1, 9)",
    "import os as o\no.system('id')", "import os\nx = os\nx.system('id')", "import sys\nsys.modules['os']",
    "import sys\nsys._getframe()", "import os\ngetattr(os, 'sys' + 'tem')('id')", "import os\ngetattr(os, 'system')",
    "g = getattr", "b = __builtins__", "__import__('os')", "eval('1')", "exec('1')", "compile('1', 'x', 'eval')",
    "globals()", "vars()", "help()", "breakpoint()", "object.__subclasses__()", "(lambda: 0).__globals__",
    "def broken(:\n",
]
ALLOWED = [
    "from __future__ import annotations", "import os\nNOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)",
    "import os\nhasattr(os, 'fchmod')", "import re\nre.compile('x')", "from . import __version__, hook",
    "from .build import run_build", "from cc_memory_index.hook import main",
    "from dataclasses import dataclass\n@dataclass\nclass C:\n    help: str\n", "f(help='x')",
    "try:\n    pass\nexcept Exception as e:\n    type(e).__name__\n", "class E(Exception):\n    def __init__(self):\n"
    "        super().__init__('x')\n", "import sys, os\nos.path.join(os.environ['X'], __file__)\nsys.exit(0)",
]


def _scan(tmp_path: Path, source: str) -> list[str]:
    sample = tmp_path / "sample.py"
    sample.write_text(source, encoding="utf-8")
    hits: list[str] = GATE["scan"]([sample])
    return hits


@pytest.mark.parametrize("source, flagged", AUDIT_SAMPLES)
def test_the_reviews_samples_are_caught_on_every_line(tmp_path: Path, source: str, flagged: set[int]) -> None:
    hits = _scan(tmp_path, source)
    prefix = len(str(tmp_path / "sample.py")) + 1
    assert {int(hit[prefix:].split(":")[0]) for hit in hits} == flagged, hits


@pytest.mark.parametrize("source", BYPASSES)
def test_bypass_is_caught(tmp_path: Path, source: str) -> None:
    assert _scan(tmp_path, source), source


@pytest.mark.parametrize("source", ALLOWED)
def test_what_the_package_needs_is_allowed(tmp_path: Path, source: str) -> None:
    assert _scan(tmp_path, source) == []


def test_the_package_and_the_plugin_hook_pass(tmp_path: Path) -> None:
    done = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "scripts", "check_forbidden.py")],
                          cwd=str(tmp_path), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout
    assert done.stdout == "forbidden-API gate: 0 finding(s) in cc_memory_index/, hooks/\n"


def test_findings_fail_the_gate(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("import socket\n", encoding="utf-8")
    done = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "scripts", "check_forbidden.py"), str(tmp_path)],
                          cwd=str(tmp_path), capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and "bad.py:1: module 'socket' is not allowed" in done.stdout
