#!/usr/bin/env bash
# 작성자 : 최태성
# 담당 영역 : ingestion
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> Running format from $ROOT_DIR"

if [ -f "./pyproject.toml" ] || [ -f "./requirements.txt" ]; then
  echo ""
  echo "==> running python format"
  if command -v ruff >/dev/null 2>&1; then
    ruff format .
    ruff check . --fix
  elif command -v black >/dev/null 2>&1; then
    black .
  else
    echo "ruff/black not found. Skipping root Python format."
  fi
fi

echo ""
echo "==> Format completed"
