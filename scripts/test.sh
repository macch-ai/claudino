#!/usr/bin/env bash
# Run the test suite in the repo's .venv, creating it with uv when missing.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  uv venv -q .venv
fi
uv pip install -q --python .venv/bin/python -r requirements.txt
exec .venv/bin/python -m unittest discover -s tests -t . -v "$@"
