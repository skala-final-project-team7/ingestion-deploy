#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> Running full verification from $ROOT_DIR"

echo ""
echo "==> Step 1/3: format"
./scripts/format.sh

echo ""
echo "==> Step 2/3: lint"
./scripts/lint.sh

echo ""
echo "==> Step 3/3: test"
./scripts/test.sh

echo ""
echo "==> Verification completed successfully"
