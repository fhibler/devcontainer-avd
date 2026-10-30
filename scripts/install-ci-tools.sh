#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/versions.env
source scripts/versions.env
python3 -m venv .tools/venv
.tools/venv/bin/pip install "ruff==$RUFF_VERSION" "PyYAML==$PY_YAML_VERSION" "jsonschema==$JSONSCHEMA_VERSION"
npm install --prefix .tools/npm "markdownlint-cli@$MARKDOWNLINT_VERSION"
mkdir -p .tools/bin
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL --retry 2 --max-time 120 "https://github.com/koalaman/shellcheck/releases/download/v${SHELLCHECK_VERSION}/shellcheck-v${SHELLCHECK_VERSION}.linux.x86_64.tar.xz" -o "$work/shellcheck.tar.xz"
tar -xJf "$work/shellcheck.tar.xz" -C "$work"
cp "$work/shellcheck-v${SHELLCHECK_VERSION}/shellcheck" .tools/bin/
curl -fsSL --retry 2 --max-time 120 "https://github.com/mvdan/sh/releases/download/v${SHFMT_VERSION}/shfmt_v${SHFMT_VERSION}_linux_amd64" -o .tools/bin/shfmt
curl -fsSL --retry 2 --max-time 120 "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" -o "$work/actionlint.tar.gz"
tar -xzf "$work/actionlint.tar.gz" -C .tools/bin actionlint
chmod +x .tools/bin/*
