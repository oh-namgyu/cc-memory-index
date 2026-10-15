"""One version everywhere: the package, the Claude Code plugin manifest and the changelog."""

from __future__ import annotations

import json
import os
import re

from cc_memory_index import __version__
from conftest import ROOT


def _read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def test_plugin_manifest_has_the_package_version() -> None:
    assert json.loads(_read(".claude-plugin", "plugin.json"))["version"] == __version__


def test_pyproject_takes_the_version_from_the_package() -> None:
    tables = dict(re.findall(r"(?ms)^\[([\w.]+)\]\n(.*?)(?=^\[|\Z)", _read("pyproject.toml")))
    assert 'dynamic = ["version"]' in tables["project"] and re.search(r"(?m)^version\s*=", tables["project"]) is None
    assert tables["tool.setuptools.dynamic"].strip() == 'version = { attr = "cc_memory_index.__version__" }'


def test_changelog_has_a_section_for_the_version() -> None:
    assert re.search(rf"(?m)^## \[{re.escape(__version__)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", _read("CHANGELOG.md"))
