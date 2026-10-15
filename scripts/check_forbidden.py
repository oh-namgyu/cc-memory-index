"""Fail if product code uses an API this tool promises never to use (SECURITY.md).

The tool only reads and writes files in one directory: network access, child processes and
dynamic code execution have no place in it. The check reads each file's syntax tree:

* every imported module must be in ALLOWED_MODULES, so adding one is a reviewed change of this
  file; an import from the package itself must name one of its modules; `import *` is refused;
* process and interpreter-introspection attributes (os.system, os.posix_spawn, sys.modules, ...)
  are refused on any object and in `from module import name`;
* eval, exec, compile, globals, vars, help and dunder names such as __import__ or __builtins__
  are refused; getattr, hasattr, setattr and delattr only with a literal attribute name, which
  is then checked like an attribute.

Usage: check_forbidden.py [PATH ...]  (default: the package and the plugin's hooks/)
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
from typing import Optional, Sequence, Union

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = "cc_memory_index"
ALLOWED_MODULES = frozenset({
    "__future__", "argparse", "collections", "contextlib", "dataclasses", "errno", "fcntl", "hashlib", "io",
    "itertools", "json", "msvcrt", "os", "re", "secrets", "stat", "sys", "time", "typing", PACKAGE,
})
DENIED_ATTRS = re.compile(
    r"(system|popen|startfile|kill|killpg|fork|forkpty|posix_spawnp?|exec[lv]p?e?|spawn[lv]p?e?"
    r"|modules|_getframe|settrace|setprofile)\Z"
)
DENIED_BUILTINS = frozenset({"eval", "exec", "compile", "globals", "locals", "vars", "help", "breakpoint"})
REFLECTION = frozenset({"getattr", "hasattr", "setattr", "delattr"})
ALLOWED_DUNDERS = frozenset({"__name__", "__file__", "__version__", "__all__", "__init__"})


def _dunder(name: str) -> Optional[str]:
    """Why a dunder name is refused, or None (a plain name or an allowed dunder)."""
    if name.startswith("__") and name.endswith("__") and name not in ALLOWED_DUNDERS:
        return f"dunder name {name!r}"
    return None


def _bad_name(name: str) -> Optional[str]:
    """Why an attribute or imported name is refused, or None."""
    return f"process or interpreter API {name!r}" if DENIED_ATTRS.match(name) else _dunder(name)


class Gate(ast.NodeVisitor):
    """Collects (line, reason) findings for one file; modules are the package's module names."""

    def __init__(self, modules: frozenset[str]) -> None:
        self.modules = modules
        self.findings: list[tuple[int, str]] = []

    def _flag(self, node: Union[ast.stmt, ast.expr], why: Optional[str]) -> None:
        if why is not None:
            self.findings.append((node.lineno, why))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            self._flag(node, None if top in ALLOWED_MODULES else f"module {alias.name!r} is not allowed")
            if top == PACKAGE:
                self._package(node, alias.name.split(".")[1:2], [])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        parts = (node.module or "").split(".") if node.module else []
        names = [alias.name for alias in node.names]
        if node.level > 1:
            self._flag(node, "relative import from outside the package")
        elif node.level == 1:
            self._package(node, parts[:1], names)
        elif parts[0] not in ALLOWED_MODULES:
            self._flag(node, f"module {node.module!r} is not allowed")
        elif parts[0] == PACKAGE:
            self._package(node, parts[1:2], names)
        for name in names:
            self._flag(node, "import *" if name == "*" else _bad_name(name))

    def _package(self, node: ast.stmt, sub: list[str], names: list[str]) -> None:
        """An import from the package must name one of its modules (the scanned files)."""
        for name in sub or [n for n in names if n not in ALLOWED_DUNDERS]:
            self._flag(node, None if name in self.modules else f"{name!r} is not a module of {PACKAGE}")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._flag(node, _bad_name(node.attr))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and node.id in DENIED_BUILTINS:
            self._flag(node, f"builtin {node.id!r}")
        elif node.id in REFLECTION:
            self._flag(node, f"{node.id} used other than called with a literal name")
        else:
            self._flag(node, _dunder(node.id))

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in REFLECTION:
            name = node.args[1] if len(node.args) > 1 else None
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                self._flag(node, _bad_name(name.value))
            else:
                self._flag(node, f"{func.id} with a computed attribute name")
            for child in [*node.args, *node.keywords]:
                self.visit(child)
            return
        self.generic_visit(node)


def scan_file(path: pathlib.Path, modules: frozenset[str]) -> list[str]:
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        return [f"{path}:{e.lineno}: cannot be parsed, so it cannot be checked"]
    gate = Gate(modules)
    gate.visit(tree)
    lines = text.splitlines()
    return [f"{path}:{no}: {why}: {lines[no - 1].strip() if 0 < no <= len(lines) else ''}"
            for no, why in sorted(gate.findings)]


def package_modules() -> frozenset[str]:
    return frozenset(path.stem for path in (ROOT / PACKAGE).glob("*.py"))


def scan(paths: Sequence[pathlib.Path]) -> list[str]:
    """Findings in every .py file under paths, one line each."""
    modules = package_modules()
    files = sorted(f for p in paths for f in (sorted(p.rglob("*.py")) if p.is_dir() else [p]))
    return [hit for f in files for hit in scan_file(f, modules)]


def main(argv: Sequence[str]) -> int:
    paths = [pathlib.Path(arg) for arg in argv] or [ROOT / PACKAGE, ROOT / "hooks"]
    hits = scan(paths)
    for hit in hits:
        print(hit)
    where = ", ".join(p.name + "/" if p.is_dir() else p.name for p in paths)
    print(f"forbidden-API gate: {len(hits)} finding(s) in {where}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
