"""Fail if product code uses an API this tool promises never to use.

The tool only reads and writes files in one directory. Network access, child processes and
dynamic code execution have no place in it, so any of them appearing is a review finding.
"""
from __future__ import annotations

import pathlib
import re
import sys

FORBIDDEN = {
    r"\bimport\s+(subprocess|socket|ssl|pickle|marshal|shelve|ctypes|yaml)\b": "forbidden module",
    r"\bfrom\s+(subprocess|socket|ssl|pickle|marshal|shelve|ctypes|yaml|urllib|http)\b": "forbidden module",
    r"\bimport\s+(urllib|http)\b": "network module",
    r"(?<![.\w])(eval|exec|compile|__import__)\s*\(": "dynamic code execution",
    r"\bos\.(system|popen|exec\w*|spawn\w*|fork)\b": "process creation",
}


def scan(root: pathlib.Path) -> list[str]:
    hits = []
    for path in sorted(root.rglob("*.py")):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            for pattern, why in FORBIDDEN.items():
                if re.search(pattern, code):
                    hits.append(f"{path}:{no}: {why}: {line.strip()}")
    return hits


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent / "cc_memory_index"
    hits = scan(root)
    for hit in hits:
        print(hit)
    print(f"forbidden-API gate: {len(hits)} finding(s) in {root.name}/")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
