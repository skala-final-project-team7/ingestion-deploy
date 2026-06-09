#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> Running tests from $ROOT_DIR"

run_if_exists() {
  local path="$1"
  shift

  if [ -e "$path" ]; then
    echo ""
    echo "==> $*"
    "$@"
  fi
}

# Root Java project
if [ -f "./gradlew" ]; then
  echo ""
  echo "==> ./gradlew test"
  ./gradlew test
elif [ -f "./mvnw" ]; then
  echo ""
  echo "==> ./mvnw test"
  ./mvnw test
fi

# Root Python project (단독 RAG / 단일 모듈 저장소)
if [ -f "./pyproject.toml" ] || [ -f "./requirements.txt" ]; then
  echo ""
  echo "==> root python tests"
  if command -v pytest >/dev/null 2>&1; then
    pytest
  else
    echo "pytest not found. Install pytest or adjust scripts/test.sh."
  fi
fi

# Backend Java project
if [ -f "./backend/gradlew" ]; then
  echo ""
  echo "==> backend tests"
  (cd backend && ./gradlew test)
elif [ -f "./backend/mvnw" ]; then
  echo ""
  echo "==> backend tests"
  (cd backend && ./mvnw test)
fi

# Frontend Node project
if [ -f "./frontend/package.json" ]; then
  echo ""
  echo "==> frontend tests"
  if [ -f "./frontend/pnpm-lock.yaml" ]; then
    (cd frontend && pnpm test -- --runInBand || pnpm test)
  elif [ -f "./frontend/yarn.lock" ]; then
    (cd frontend && yarn test)
  else
    (cd frontend && npm test)
  fi
fi

# RAG Pipeline Python project
if [ -f "./rag-pipeline/pyproject.toml" ] || [ -f "./rag-pipeline/requirements.txt" ]; then
  echo ""
  echo "==> rag-pipeline tests"
  if command -v pytest >/dev/null 2>&1; then
    (cd rag-pipeline && pytest)
  else
    echo "pytest not found. Install pytest or adjust scripts/test.sh."
  fi
fi

# AI Agent Python project
if [ -f "./ai-agent/pyproject.toml" ] || [ -f "./ai-agent/requirements.txt" ]; then
  echo ""
  echo "==> ai-agent tests"
  if command -v pytest >/dev/null 2>&1; then
    (cd ai-agent && pytest)
  else
    echo "pytest not found. Install pytest or adjust scripts/test.sh."
  fi
fi

echo ""
echo "==> Tests completed"
