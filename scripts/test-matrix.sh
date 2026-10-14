#!/usr/bin/env bash
# Run the test suite on the host Python and on Python 3.9 / 3.12 in throwaway containers.
# The repository is mounted read-only; nothing is installed on the host.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
echo "== host: $("$PY" --version)"
"$PY" -m pytest -q -p no:cacheprovider tests/
if command -v docker >/dev/null 2>&1; then
  for v in 3.9 3.12; do
    echo "== docker python:$v-slim"
    docker run --rm -v "$PWD":/src:ro -w /tmp python:"$v"-slim sh -c \
      "cp -r /src /tmp/w && cd /tmp/w && pip install -q --disable-pip-version-check pytest && python -m pytest -q -p no:cacheprovider tests/"
  done
else
  echo "docker not found: skipped the 3.9 / 3.12 container runs" >&2
fi
