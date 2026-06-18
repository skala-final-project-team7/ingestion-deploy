#!/usr/bin/env bash
# 작성자 : 최태성
# 담당 영역 : ingestion
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> Running lint checks from $ROOT_DIR"

if [ -f "./pyproject.toml" ] || [ -f "./requirements.txt" ]; then
  echo ""
  echo "==> root python lint"
  if command -v ruff >/dev/null 2>&1; then
    ruff check .
  else
    echo "ruff not found. Skipping ruff for root Python project."
  fi

  if command -v mypy >/dev/null 2>&1; then
    mypy app
  else
    echo "mypy not found. Skipping mypy for root Python project."
  fi
fi

echo ""
echo "==> Lint completed"
