# Contributing

- Python 3.9+ and the standard library only: no runtime dependencies. Tests use pytest.
- Run `python3 -m pytest -q tests/` before sending a change; `scripts/test-matrix.sh` also runs the
  suite on Python 3.9 and 3.12 in Docker. CI runs Linux, macOS and Windows.
- `python3 scripts/check_forbidden.py` must report 0 findings for `cc_memory_index/` and `hooks/`.
  Only the standard-library modules in its `ALLOWED_MODULES` may be imported; a new import is a
  reviewed change to that list.
- CI also runs `python -m pyflakes cc_memory_index hooks scripts tests` and
  `python -m mypy --strict --python-version 3.9 cc_memory_index` (mypy 1.20.2, the last release
  that accepts a 3.9 target).
- Every write goes through `StoreDir.replace` (or `remove`) with the fingerprint taken when the
  file was read. A new writing path needs a fault-injection test in `tests/test_faults*.py` showing
  that a failure at each of its writes, followed by a rerun, converges with nothing lost.
- Memory content is untrusted. Text that reaches an index line goes through `text.sanitize`.
- [docs/DESIGN.md](docs/DESIGN.md) is the contract. Change it in the same pull request as the
  behaviour it describes.
- Keep files under 300 lines and functions under 50 lines.
- Tests use synthetic stores only. Never commit real memory content.
