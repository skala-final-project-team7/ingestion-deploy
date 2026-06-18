#!/usr/bin/env bash
# 작성자 : 최태성
# 담당 영역 : ingestion
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> Running tests from $ROOT_DIR"

if [ -f "./pyproject.toml" ] || [ -f "./requirements.txt" ]; then
  echo ""
  echo "==> root python tests"
  if command -v pytest >/dev/null 2>&1; then
    pytest
  else
    echo "pytest not found. Install pytest or adjust scripts/test.sh."
  fi
fi

echo ""
echo "==> Tests completed"
