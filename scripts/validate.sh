#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/.tools/bin:$PWD/.tools/venv/bin:$PWD/.tools/npm/node_modules/.bin:$PATH"
ruff check src scripts test
ruff format --check src scripts test
python -m unittest discover -s test/unit -v
python scripts/check.py
shellcheck -x src/avd/install.sh src/avd/avd-command scripts/*.sh
shfmt -d -i 4 src/avd/install.sh src/avd/avd-command scripts/*.sh
actionlint
markdownlint README.md src/avd/README.md 'docs/**/*.md' examples/*/README.md
