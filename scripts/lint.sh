#!/usr/bin/env bash
# 작성자 : 최태성
# 담당 영역 : ingestion
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> Running lint checks from $ROOT_DIR"

# Root Java project
if [ -f "./gradlew" ]; then
  echo ""
  echo "==> ./gradlew check"
  ./gradlew check
elif [ -f "./mvnw" ]; then
  echo ""
  echo "==> ./mvnw verify -DskipTests"
  ./mvnw verify -DskipTests
fi

# Root Python project (단독 RAG / 단일 모듈 저장소)
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

# Backend Java project
if [ -f "./backend/gradlew" ]; then
  echo ""
  echo "==> backend lint/check"
  (cd backend && ./gradlew check)
elif [ -f "./backend/mvnw" ]; then
  echo ""
  echo "==> backend lint/check"
  (cd backend && ./mvnw verify -DskipTests)
fi

# Frontend Node project
if [ -f "./frontend/package.json" ]; then
  echo ""
  echo "==> frontend lint"
  if [ -f "./frontend/pnpm-lock.yaml" ]; then
    (cd frontend && pnpm lint)
  elif [ -f "./frontend/yarn.lock" ]; then
    (cd frontend && yarn lint)
  else
    (cd frontend && npm run lint)
  fi
fi

# Python projects
for dir in rag-pipeline ai-agent; do
  if [ -d "./$dir" ] && { [ -f "./$dir/pyproject.toml" ] || [ -f "./$dir/requirements.txt" ]; }; then
    echo ""
    echo "==> $dir lint"

    if command -v ruff >/dev/null 2>&1; then
      (cd "$dir" && ruff check .)
    else
      echo "ruff not found. Skipping ruff for $dir."
    fi

    if command -v mypy >/dev/null 2>&1; then
      (cd "$dir" && mypy .)
    else
      echo "mypy not found. Skipping mypy for $dir."
    fi
  fi
done

echo ""
echo "==> Lint completed"
